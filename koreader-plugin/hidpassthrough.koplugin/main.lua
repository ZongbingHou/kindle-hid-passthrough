--[[--
管理 kindle-hid-passthrough 守护进程并直接在 KOReader 内部映射按键。

@module koplugin.hidpassthrough
--]]

local ConfirmBox = require("ui/widget/confirmbox")
local DataStorage = require("datastorage")
local Device = require("device")
local Dispatcher = require("dispatcher")
local InfoMessage = require("ui/widget/infomessage")
local InputContainer = require("ui/widget/container/inputcontainer")
local LuaSettings = require("luasettings")
local Menu = require("ui/widget/menu")
local PluginShare = require("pluginshare")
local PowerD = Device:getPowerDevice()
local Screen = require("device").screen
local TextViewer = require("ui/widget/textviewer")
local UIManager = require("ui/uimanager")
local logger = require("logger")
local time = require("ui/time")
local rapidjson = require("rapidjson")
local util = require("util")
local ffiutil = require("ffi/util")
local _ = require("gettext")
local T = require("ffi/util").template

local socket = require("socket")
local http = require("socket.http")
local ltn12 = require("ltn12")

local lfs = require("libs/libkoreader-lfs")
local ffi = require("ffi")
local C = ffi.C
local bit = require("bit")
pcall(require, "ffi/posix_h")
pcall(require, "ffi/fbink_input_h")

local HIDPassthrough = InputContainer:extend{
    name = "hidpassthrough",
    is_doc_only = false,

    -- 若安装位置不同，请在 settings/hidpassthrough.lua 中进行重写。
    DAEMON_BINARY = "/mnt/us/kindle_hid_passthrough/kindle-hid-passthrough",
    API_HOST      = "127.0.0.1",
    API_PORT      = 8321,
    API_TIMEOUT   = 2, -- 单位：秒
}

------------------------------------------------------------------------------
-- HTTP 辅助函数
------------------------------------------------------------------------------

-- 返回响应体或 (nil, err)。
function HIDPassthrough:_httpGet(path)
    local url = string.format("http://%s:%d%s", self.API_HOST, self.API_PORT, path)
    local body_chunks = {}

    -- socket.http.TIMEOUT 是模块全局变量，因此需要先保存并在之后恢复。
    local saved_timeout = http.TIMEOUT
    http.TIMEOUT = self.API_TIMEOUT

    local ok, code = http.request{
        url = url,
        sink = ltn12.sink.table(body_chunks),
        create = function()
            local s = socket.tcp()
            s:settimeout(self.API_TIMEOUT)
            return s
        end,
    }

    http.TIMEOUT = saved_timeout

    if not ok then
        return nil, tostring(code)
    end
    if code ~= 200 then
        return nil, "HTTP " .. tostring(code)
    end
    return table.concat(body_chunks)
end

function HIDPassthrough:_httpGetJson(path)
    local body, err = self:_httpGet(path)
    if not body then return nil, err end
    local data, perr = rapidjson.decode(body)
    if not data then return nil, "JSON 解码失败: " .. tostring(perr) end
    return data, nil
end

------------------------------------------------------------------------------
-- 守护进程状态
------------------------------------------------------------------------------
-- "off" = API 服务未启动，"api_only" = 服务已启动但守护进程未运行，
-- "on" = 两者皆已启动。启动二进制文件会同时唤醒这两个层级。

HIDPassthrough.START_TIMEOUT = 15
HIDPassthrough.STOP_TIMEOUT = 5
-- 守护进程在每次重连时都会销毁并重建 UHID 节点，而 KOReader 的 uevent 热插拔
-- 可能错过或与新节点产生竞争；此外 externalkeyboard 在挂载键盘时还会整体替换
-- event_map。因此用一个较慢的周期性重扫让插件实现自愈，避免按键在重连/重启后失效。
HIDPassthrough.RESCAN_INTERVAL = 4

-- 返回状态 state 和响应体 body，状态为 "off" / "api_only" / "on" 之一。
function HIDPassthrough:getState()
    local body, err = self:_httpGet("/status")
    if not body then
        logger.dbg("HIDPassthrough: 无法连接至 API:", err)
        return "off", nil
    end
    if body:find('"daemon_running"%s*:%s*true') then
        return "on", body
    end
    return "api_only", body
end

function HIDPassthrough:isRunning()
    return self:getState() == "on"
end

------------------------------------------------------------------------------
-- 按键映射
------------------------------------------------------------------------------
-- 未基于 hotkeys.koplugin 构建：除非存在 hasScreenKB 或 hasKeyboard
--（现代 Kindle 设备均未设置），否则它会返回禁用，且 PluginLoader 会对其进行缓存。

local MODIFIER_KEYS = {
    Shift = true, Ctrl = true, Alt = true, Meta = true, Sym = true,
    ScreenKB = true, LCtrl = true, LAlt = true, RAlt = true, LMeta = true,
    RMeta = true, CapsLock = true,
}

-- 固定顺序，以便给定的组合键始终序列化为相同的 ID。
local MOD_ORDER = { "Shift", "Ctrl", "Alt", "Meta", "Sym", "ScreenKB" }

-- 快捷动作列表，避免常用绑定需要在完整的动作树中逐层翻找。
-- 始终通过 Dispatcher 键名而非标题引用，以兼容名称变更和翻译。
local COMMON_ACTIONS = {
    "hidpassthrough_next_page",
    "hidpassthrough_prev_page",
    "hidpassthrough_close",
    "toggle_frontlight",
    "night_mode",
    "show_menu",
    "toc",
    "bookmarks",
    "toggle_bookmark",
    "iterate_rotation",
    "back",
}

-- 例如 "F13", "Shift+F13"。从 Key 哈希中读取修饰键，而非 key.modifiers
--（后者是对 Input 表的实时引用，而非快照）。
local function keyToId(key)
    local parts = {}
    for dummy, mod in ipairs(MOD_ORDER) do -- luacheck: ignore dummy
        if key[mod] then table.insert(parts, mod) end
    end
    table.insert(parts, key.key)
    return table.concat(parts, "+")
end

local function idToSequence(id)
    local seq = {}
    for part in id:gmatch("[^+]+") do table.insert(seq, part) end
    return seq
end

-- 在 文件管理器 (FileManager) 和 阅读器 (ReaderUI) 插件实例之间共享。
local keymap_path = ffiutil.joinPath(DataStorage:getSettingsDir(),
    "hidpassthrough_keymap.lua")
local keymap_settings

local function getKeymapSettings()
    if not keymap_settings then
        keymap_settings = LuaSettings:open(keymap_path)
    end
    return keymap_settings
end

-- 声明于 _attachInput 上方，由其填充内容。
local input_fds = {}

-- KOReader 会丢弃 code 不在 event_map 中的 EV_KEY 事件。
-- 此处通过追加方式补全缺失映射；由于外接键盘插件（externalkeyboard）在挂载时会替换整个映射表，
-- 并在卸载时恢复快照，因此在连接/断开时会重新应用。extra 表会缓存：
-- 它还会被定时器反复应用，每次重新编译纯属浪费。
local event_map_extra_cache

function HIDPassthrough:_extendEventMap()
    local map = Device.input and Device.input.event_map
    if not map then
        self._event_map_status = _("KOReader 未暴露输入事件映射表")
        return
    end

    if not event_map_extra_cache then
        local path = ffiutil.joinPath(self.path, "event_map_extra.lua")
        local ok, extra = pcall(dofile, path)
        if not ok or type(extra) ~= "table" then
            logger.warn("HIDPassthrough: 无法加载 event_map_extra:", extra)
            self._event_map_status = T(_("加载失败：%1"), path)
            return
        end
        event_map_extra_cache = extra
    end

    local added = 0
    for code, name in pairs(event_map_extra_cache) do
        if map[code] == nil then
            map[code] = name
            added = added + 1
        end
    end
    self._event_map_status = T(_("已注册 %1 个额外键码"), tostring(added))
    logger.dbg("HIDPassthrough: 已向事件映射表添加", added, "个额外键码")
end

------------------------------------------------------------------------------
-- 按键设备挂载
------------------------------------------------------------------------------
-- externalkeyboard 会匹配 INPUT_KEYBOARD，但 FBInk 仅在 1..31 的键码全部存在时才会设置它，
-- 因此遥控器或游戏手柄不会被任何模块自动打开。

-- 延迟加载：上方的 fbink_input cdef 处于 pcall 中，如果在模块作用域直接访问 C.INPUT_*，
-- 当其不存在时会导致插件崩溃。
local exclude_types
local function excludeTypes()
    if not exclude_types then
        exclude_types = bit.bor(
            C.INPUT_KEYBOARD,
            C.INPUT_TOUCHSCREEN,
            C.INPUT_TABLET,
            C.INPUT_SCALED_TABLET,
            C.INPUT_ACCELEROMETER,
            C.INPUT_ROTATION_EVENT,
            C.INPUT_KINDLE_FRAME_TAP,
            C.INPUT_POWER_BUTTON,
            C.INPUT_SLEEP_COVER)
    end
    return exclude_types
end

-- 包含 INPUT_JOYSTICK：FBInk 的 test_key 仅检测 BTN_MISC 以下以及 KEY_OK..BTN_TRIGGER_HAPPY 范围，
-- 因此仅包含 BTN_A..BTN_THUMBR (304-319) 的手柄永远不会被识别为 INPUT_KEY。
local match_types
local function matchTypes()
    if not match_types then
        match_types = bit.bor(C.INPUT_KEY, C.INPUT_JOYSTICK)
    end
    return match_types
end

local function checkKeyDevice(path)
    local FBInkInput = ffi.loadlib("fbink_input", 1)
    local dev = FBInkInput.fbink_input_check(path, matchTypes(), excludeTypes(), 0)
    local info
    if dev ~= nil then
        if dev.matched then
            info = {
                fd   = tonumber(dev.fd),
                path = ffi.string(dev.path),
                name = ffi.string(dev.name),
            }
        else
            -- 核心防护 1：如果 FBInk 打开了节点但未匹配，必须释放底层 fd，防止 FBInk 泄漏
            if dev.fd and dev.fd >= 0 then
                C.close(dev.fd)
            end
        end
        C.free(dev)
    end
    return info
end

function HIDPassthrough:_attachInput(path, force)
    if input_fds[path] and not force then return end
    if Device.input and Device.input.opened_devices and Device.input.opened_devices[path] and not input_fds[path] then
        return
    end

    local info = checkKeyDevice(path)
    if not info then return end

    -- 若设备重新连接，先尝试清理旧句柄
    if input_fds[info.path] then
        pcall(Device.input.close, Device.input, info.path)
        input_fds[info.path] = nil
    end

    -- 核心防护 2：使用 pcall 包裹 fdopen。防止槽位爆满抛出 Error 时导致 fd 永远无法 close
    local res = nil
    local success, err = pcall(function()
        res = Device.input:fdopen(info.fd, info.path, info.name)
    end)

    if not success or not res then
        logger.warn("HIDPassthrough: 挂载输入设备失败，强制释放系统 fd ->", info.path, err or "nil response")
        -- 出现错误时，手动关闭 FBInk 打开的句柄
        if info.fd and info.fd >= 0 then
            C.close(info.fd)
        end
        return
    end

    input_fds[info.path] = res
    logger.info("HIDPassthrough: 已挂载输入设备", info.name, "@", info.path)

    self:_extendEventMap()
    self:registerKeyEvents()
end

function HIDPassthrough:_detachInput(path)
    if not input_fds[path] then return end
    Device.input:close(path)
    input_fds[path] = nil
    logger.info("HIDPassthrough: 已卸载输入设备", path)
end

function HIDPassthrough:_scanInputs()
    for name in lfs.dir("/dev/input") do
        -- 核心防护 3：跳过 event0（触摸屏/原生按键节点），防止误抢占或重复打开
        if name:match("^event%d+$") and name ~= "event0" then
            self:_attachInput("/dev/input/" .. name)
        end
    end
end

-- 清理我们打开但磁盘上已不存在的输入节点。守护进程重连时会销毁 UHID 节点；
-- 若对应的 InputRemove uevent 被遗漏，残留的 fd 会让 input poll 以 ENODEV 失败，
-- 导致所有插件（乃至 KOReader 自身）都收不到按键。
function HIDPassthrough:_pruneInputs()
    local live = {}
    for name in lfs.dir("/dev/input") do
        if name:match("^event%d+$") then
            live["/dev/input/" .. name] = true
        end
    end
    for path in pairs(input_fds) do
        if not live[path] then
            logger.info("HIDPassthrough: 清理失效输入节点", path)
            self:_detachInput(path)
        end
    end
end

-- 自愈循环：重新挂载被重建的 UHID 节点、重新应用事件映射
-- （externalkeyboard 挂载键盘时会替换它）、并重新注册绑定。
function HIDPassthrough:_scheduleRescan()
    UIManager:scheduleIn(self.RESCAN_INTERVAL, function()
        if self._closing then return end
        self:_pruneInputs()
        self:_scanInputs()
        self:_extendEventMap()
        self:registerKeyEvents()
        self:_scheduleRescan()
    end)
end

local type_names
local function typeNames()
    if not type_names then
        type_names = {
            { C.INPUT_POINTINGSTICK, "指点杆" },
            { C.INPUT_MOUSE, "鼠标" },
            { C.INPUT_TOUCHPAD, "触摸板" },
            { C.INPUT_TOUCHSCREEN, "触摸屏" },
            { C.INPUT_JOYSTICK, "摇杆/手柄" },
            { C.INPUT_TABLET, "数位板" },
            { C.INPUT_KEY, "按键" },
            { C.INPUT_KEYBOARD, "键盘" },
            { C.INPUT_ACCELEROMETER, "加速度计" },
            { C.INPUT_DPAD, "方向键" },
            { C.INPUT_VOLUME_BUTTONS, "音量键" },
        }
    end
    return type_names
end

local function describeInput(path)
    local FBInkInput = ffi.loadlib("fbink_input", 1)
    local dev = FBInkInput.fbink_input_check(path, C.INPUT_KEY, 0, C.SCAN_ONLY)
    if dev == nil then return nil end
    local name, dtype = ffi.string(dev.name), dev.type
    C.free(dev)

    local types = {}
    for dummy, pair in ipairs(typeNames()) do -- luacheck: ignore dummy
        if bit.band(dtype, pair[1]) ~= 0 then table.insert(types, pair[2]) end
    end
    return name, #types > 0 and table.concat(types, ",") or "未知"
end

function HIDPassthrough:showInputDiagnostics()
    local lines = {
        T(_("额外事件映射：%1"), self._event_map_status or _("未加载")),
        T(_("插件目录：%1"), tostring(self.path)),
        "",
        _("输入设备列表："),
    }

    local paths = {}
    for name in lfs.dir("/dev/input") do
        if name:match("^event%d+$") then
            table.insert(paths, "/dev/input/" .. name)
        end
    end
    table.sort(paths)

    for dummy, path in ipairs(paths) do -- luacheck: ignore dummy
        local name, types = describeInput(path)
        local owner
        if input_fds[path] then
            owner = _("已打开 (本插件)")
        elseif Device.input.opened_devices[path] then
            owner = _("已打开 (KOReader)")
        else
            owner = _("未打开 - 按键将被忽略")
        end
        table.insert(lines, T("%1  %2\n    [%3]  %4",
            path, name or "?", types or "?", owner))
    end

    UIManager:show(TextViewer:new{
        title = _("输入设备诊断信息"),
        text = table.concat(lines, "\n"),
        justified = false,
    })
end

-- 插入设备始终代表新设备，因此强制重新打开。
-- 延迟 1 秒（而非 externalkeyboard 的 0.5 秒），以确保实体键盘优先获得处理。
function HIDPassthrough:onEvdevInputInsert(path)
    UIManager:scheduleIn(1, function() self:_attachInput(path, true) end)
end

function HIDPassthrough:onEvdevInputRemove(path)
    UIManager:scheduleIn(1, function() self:_detachInput(path) end)
end

-- 翻页键：KOReader 按物理左右侧（L/R）命名，并会在设备旋转时交换
-- PgFwd<->PgBack。蓝牙翻页器发送的是相同的原始键码（KEY_PAGEUP=104 /
-- KEY_PAGEDOWN=109），因此只要平台映射表（或 externalkeyboard 整体替换映射表）
-- 改了名字，为 "LPgFwd" 建立的绑定就会失配。这里把裸翻页键绑定注册为整个
-- PgFwd/PgBack 组（Key:match 支持 {a, b} 备选），从而 L<->R 改名仍能触发绑定。
-- 当同组两个成员都被绑定时会相互冲突，故保持原样；旋转导致的 Fwd<->Back 对调
-- 由菜单中的 "交换翻页键"（Swap page keys）处理。
local PAGE_KEY_GROUPS = {
    LPgFwd  = { "LPgFwd", "RPgFwd" },
    RPgFwd  = { "LPgFwd", "RPgFwd" },
    LPgBack = { "LPgBack", "RPgBack" },
    RPgBack = { "LPgBack", "RPgBack" },
}

function HIDPassthrough:registerKeyEvents()
    self.key_events = {}
    local keymap = getKeymapSettings().data
    -- 注册所有 ID（包括未分配动作的）：动作在按下时实时解析，
    -- 因此修改映射无需重新注册事件。
    for id in pairs(keymap) do
        local seq = idToSequence(id)
        local key = seq[#seq]
        local group = PAGE_KEY_GROUPS[key]
        if group and #seq == 1 then
            local sibling = group[1] == key and group[2] or group[1]
            if not keymap[sibling] then
                seq = { group }
            end
        end
        self.key_events["HIDPassthroughKey_" .. id] = {
            seq,
            event = "HIDPassthroughKeyAction",
            args = id,
        }
    end
    logger.dbg("HIDPassthrough: 已注册",
        util.tableSize(self.key_events), "个按键绑定")
end

function HIDPassthrough:onPhysicalKeyboardConnected()
    self:_extendEventMap()
    self:registerKeyEvents()
end

-- 覆盖 InputContainer 的处理函数（原处理函数会直接丢弃 key_events）。
function HIDPassthrough:onPhysicalKeyboardDisconnected()
    self:_extendEventMap()
    if Device:hasKeys() or next(input_fds) ~= nil then
        self:registerKeyEvents()
    else
        self.key_events = {}
    end
end

function HIDPassthrough:onHIDPassthroughKeyAction(id)
    local actions = getKeymapSettings().data[id]
    if type(actions) ~= "table" or next(actions) == nil then return end
    logger.dbg("HIDPassthrough: 正在执行按键绑定：", id)
    Dispatcher:execute(actions)
    return true
end

-- 覆盖 onKeyPress 可以绕过 key_events 匹配，使得未绑定的按键在此处可见。
local KeyCapture = InfoMessage:extend{
    on_key_captured = nil,
}

function KeyCapture:onKeyPress(key)
    if MODIFIER_KEYS[key.key] then return true end
    -- 关闭窗口前进行序列化。
    local id = keyToId(key)
    local callback = self.on_key_captured
    UIManager:close(self)
    if callback then callback(id) end
    return true
end

function HIDPassthrough:captureKey(callback)
    UIManager:show(KeyCapture:new{
        text = _("请按下您想要映射的按键。\n\n点击屏幕任意位置可取消。"),
        on_key_captured = callback,
    })
end

function HIDPassthrough:genKeymapMenu()
    local keymap = getKeymapSettings().data
    local items = {
        {
            text = _("添加按键…"),
            keep_menu_open = true,
            callback = function(touchmenu_instance)
                self:captureKey(function(id)
                    if keymap[id] == nil then
                        keymap[id] = {}
                        self.updated = true
                        self:registerKeyEvents()
                    end
                    -- updateItems() 会重新渲染缓存的 item_table，
                    -- 但不会重新运行 sub_item_table_func。
                    if touchmenu_instance then
                        touchmenu_instance.item_table = self:genKeymapMenu()
                        touchmenu_instance:updateItems()
                    end
                    UIManager:show(InfoMessage:new{
                        text = T(_("已捕获按键 %1。请在下方为其选择对应动作。"), id),
                        timeout = 3,
                    })
                end)
            end,
            separator = true,
        },
        {
            text = _("交换翻页键"),
            keep_menu_open = true,
            callback = function()
                self:_swapPageKeys()
            end,
            help_text = _("交换所有已映射翻页键的 PgFwd/PgBack 动作。"
                .. "当旋转设备或重启 KOReader 后出现上下翻页对调时使用。"),
            separator = true,
        },
    }

    local ids = {}
    for id in pairs(keymap) do table.insert(ids, id) end
    table.sort(ids)

    -- 未使用 `for _, id`，避免与这些闭包中的 gettext 别名 `_` 产生冲突。
    for dummy, id in ipairs(ids) do -- luacheck: ignore dummy
        table.insert(items, {
            text_func = function()
                local actions = keymap[id]
                local label = (actions and next(actions) ~= nil)
                    and Dispatcher:menuTextFunc(actions)
                    or _("未分配动作")
                return T("%1  →  %2", id, label)
            end,
            -- 按需展开：每一项都是完整的 Dispatcher 动作树。
            sub_item_table_func = function() return self:genKeyActionMenu(id) end,
            -- 在动作树的最底部也有删除选项，但需要多进两层菜单。
            hold_callback = function(touchmenu_instance)
                UIManager:show(ConfirmBox:new{
                    text = T(_("确定要移除 %1 的按键映射吗？"), id),
                    ok_text = _("移除"),
                    ok_callback = function()
                        self:removeKey(id)
                        if touchmenu_instance then
                            touchmenu_instance.item_table = self:genKeymapMenu()
                            touchmenu_instance:updateItems()
                        end
                    end,
                })
            end,
            ignored_by_menu_search = true,
        })
    end

    if #ids == 0 then
        table.insert(items, {
            text = _("(暂未映射任何按键)"),
            enabled = false,
        })
    end

    -- 当子菜单将我们标记为过期时，由 TouchMenu:backToUpperMenu 调用。
    items.refresh_func = function() return self:genKeymapMenu() end
    return items
end

function HIDPassthrough:removeKey(id)
    getKeymapSettings().data[id] = nil
    self.updated = true
    self:registerKeyEvents()
end

-- KOReader 的 rotation_map 会在设备旋转（或在不同方向下重启）时交换
-- PgFwd<->PgBack 的名字，导致所有已映射翻页键的方向反了。交换翻页键的动作即可
-- 一键纠正；交换结果会持久化到 keymap 文件中。
function HIDPassthrough:_swapPageKeys()
    local keymap = getKeymapSettings().data
    local swapped = false
    local function swap(a, b)
        if keymap[a] == nil and keymap[b] == nil then return end
        keymap[a], keymap[b] = keymap[b], keymap[a]
        swapped = true
    end
    swap("LPgFwd", "LPgBack")
    swap("RPgFwd", "RPgBack")
    if swapped then
        self.updated = true
        -- 立即持久化：这是修复方向错乱的逃生通道，必须能独立扛过 KOReader 重启。
        getKeymapSettings():flush()
        self.updated = false
        self:registerKeyEvents()
        UIManager:show(InfoMessage:new{
            text = _("翻页键已交换。请翻页测试。"),
            timeout = 2,
        })
    else
        UIManager:show(InfoMessage:new{
            text = _("没有可交换的翻页键映射。"),
            timeout = 2,
        })
    end
end

function HIDPassthrough:genKeyActionMenu(id)
    local keymap = getKeymapSettings().data
    local sub_items = {}

    local unknown = _("未知项目")
    for dummy, action in ipairs(COMMON_ACTIONS) do -- luacheck: ignore dummy
        local title = Dispatcher:getNameFromItem(action, nil, true)
        if title ~= unknown then
            table.insert(sub_items, {
                text = title,
                checked_func = function()
                    return keymap[id] ~= nil and keymap[id][action] ~= nil
                end,
                callback = function(touchmenu_instance)
                    if keymap[id] == nil then keymap[id] = {} end
                    if keymap[id][action] == nil then
                        keymap[id][action] = true
                        Dispatcher._addToOrder(keymap, id, action)
                    else
                        keymap[id][action] = nil
                        Dispatcher._removeFromOrder(keymap, id, action)
                    end
                    self.updated = true
                    if touchmenu_instance then
                        touchmenu_instance:updateItems()
                    end
                end,
            })
        end
    end
    if #sub_items > 0 then
        sub_items[#sub_items].separator = true
    end

    Dispatcher:addSubMenu(self, sub_items, keymap, id)
    table.insert(sub_items, {
        text = _("移除此按键"),
        -- 保持菜单打开，否则 TouchMenu 会在回调后关闭菜单，
        -- 从而破坏我们的 backToUpperMenu 逻辑。
        keep_menu_open = true,
        callback = function(touchmenu_instance)
            self:removeKey(id)
            if touchmenu_instance then
                -- 标记为过期，以便 backToUpperMenu 通过 refresh_func 重新构建菜单。
                local stack = touchmenu_instance.item_table_stack
                local parent = stack and stack[#stack]
                if parent then parent.needs_refresh = true end
                touchmenu_instance:backToUpperMenu()
            end
        end,
    })
    return sub_items
end

function HIDPassthrough:onFlushSettings()
    if self.updated then
        getKeymapSettings():flush()
        self.updated = false
    end
end

------------------------------------------------------------------------------
-- 启动 / 停止
------------------------------------------------------------------------------

-- 以分离模式启动二进制文件。仅在 API 服务本身未运行时使用。
function HIDPassthrough:_spawnBinary()
    if not util.pathExists(self.DAEMON_BINARY) then
        return false, T(_("未在 %1 找到守护进程二进制文件。"), self.DAEMON_BINARY)
    end
    -- 使用 setsid 以便在 KOReader 退出后继续运行；忽略退出码。
    local cmd = string.format(
        "(setsid %s --daemon </dev/null >/dev/null 2>&1 &) 2>/dev/null || "
        .. "(%s --daemon </dev/null >/dev/null 2>&1 &)",
        self.DAEMON_BINARY, self.DAEMON_BINARY
    )
    logger.info("HIDPassthrough: 正在启动守护进程：", cmd)
    os.execute(cmd)
    return true
end

-- 轮询等待直至 getState() 返回目标状态，或超时。
function HIDPassthrough:_waitForState(target, timeout)
    for i = 1, timeout do
        ffiutil.sleep(1)
        local state = self:getState()
        logger.dbg("HIDPassthrough: 正在等待", target, "当前状态为", state, "第", i, "次尝试")
        if state == target then
            return true
        end
    end
    return false
end

function HIDPassthrough:start()
    local state = self:getState()

    if state == "on" then
        return true, _("HID Passthrough 守护进程已在运行中。")
    end

    if state == "off" then
        -- API 服务未启动。启动二进制文件（会同时唤醒两个层级）。
        local ok, err = self:_spawnBinary()
        if not ok then return false, err end

        if self:_waitForState("on", self.START_TIMEOUT) then
            return true, _("HID Passthrough 守护进程已成功启动。")
        end

        -- 未达到 "on" 状态。判断具体的子失败类型并回报。
        local final = self:getState()
        if final == "off" then
            return false, _("守护进程启动失败：API 服务未响应。"
                .. "请尝试在 Shell 命令行中手动运行该程序以查看错误信息。")
        end
        -- final == "api_only": API 服务正常，但 HID 守护进程未启动。
        -- 尝试最后一次调用 /start，以防它只需触发一下。
        logger.info("HIDPassthrough: API 服务正常但守护进程未运行，正在调用 /start")
        if self:_httpGet("/start") and self:_waitForState("on", self.START_TIMEOUT) then
            return true, _("HID Passthrough 守护进程已成功启动。")
        end
        return false, T(_("API 服务已响应，但 HID 守护进程未能在 %1 秒内启动。"
            .. "请检查 /var/log/hid_passthrough.log。"),
            tostring(self.START_TIMEOUT))
    end

    -- state == "api_only": 直接请求 API 服务启动守护进程。
    logger.info("HIDPassthrough: API 服务正常，正在调用 /start")
    local body, err = self:_httpGet("/start")
    if not body then
        return false, T(_("调用 API /start 失败：%1"), tostring(err))
    end
    if self:_waitForState("on", self.START_TIMEOUT) then
        return true, _("HID Passthrough 守护进程已成功启动。")
    end
    return false, T(_("/start 请求已被接受，但守护进程未能在 %1 秒内启动。"
        .. "请检查 /var/log/hid_passthrough.log。"),
        tostring(self.START_TIMEOUT))
end

function HIDPassthrough:stop()
    local state = self:getState()

    if state ~= "on" then
        -- 未运行任何进程，或者仅 API 服务在运行（符合我们期望的空闲状态）。
        -- 无论哪种情况，均无需执行操作。
        return true, _("HID Passthrough 守护进程未在运行。")
    end

    -- 请求 API 服务停止 HID 守护进程。API 服务本身保持运行（参考 BTManager 逻辑），
    -- 这样可以确保下一次调用 /start 时响应迅速。
    local body, err = self:_httpGet("/stop")
    if not body then
        return false, T(_("调用 API /stop 失败：%1"), tostring(err))
    end

    -- 等待 daemon_running 状态切换为 false。
    for i = 1, self.STOP_TIMEOUT do
        ffiutil.sleep(1)
        if self:getState() ~= "on" then
            return true, _("HID Passthrough 守护进程已停止。")
        end
        logger.dbg("HIDPassthrough: 正在等待停止，第", i, "次尝试")
    end
    return false, _("守护进程未能在超时时间内停止。")
end

function HIDPassthrough:toggle()
    if self:isRunning() then
        return self:stop()
    else
        return self:start()
    end
end

------------------------------------------------------------------------------
-- 信息对话框：从 /status 中解析部分字段用于展示
------------------------------------------------------------------------------

local function extractField(body, key)
    if not body then return nil end
    -- 先尝试匹配字符串值。
    local v = body:match('"' .. key .. '"%s*:%s*"([^"]*)"')
    if v then return v end
    -- 再尝试匹配数字 / 布尔值。
    v = body:match('"' .. key .. '"%s*:%s*([%w%.%-]+)')
    return v
end

local function countDevices(body)
    if not body then return nil end
    -- 统计 "devices" 数组内部的左花括号数量。
    local arr = body:match('"devices"%s*:%s*(%b[])')
    if not arr then return nil end
    local n = 0
    for _ in arr:gmatch("{") do n = n + 1 end
    return n
end

function HIDPassthrough:showInfo()
    local state, body = self:getState()
    local lines = {}

    if state == "on" then
        table.insert(lines, _("状态：HID 守护进程运行中"))
    elseif state == "api_only" then
        table.insert(lines, _("状态：API 服务正常，HID 守护进程已停止"))
    else
        table.insert(lines, _("状态：未运行"))
    end

    if body then
        local version = extractField(body, "version")
        if version then
            table.insert(lines, T(_("版本：%1"), version))
        end

        local n_devices = countDevices(body)
        if n_devices then
            table.insert(lines, T(_("已配置设备：%1"), tostring(n_devices)))
        end

        local connected = extractField(body, "connected_device")
        if connected and connected ~= "" and connected ~= "null" then
            table.insert(lines, T(_("已连接：%1"), connected))
        end

        if body:find('"scanning"%s*:%s*true') then
            table.insert(lines, _("正在扫描中…"))
        end
        if body:find('"pairing"%s*:%s*true') then
            table.insert(lines, _("正在配对中…"))
        end
    end

    table.insert(lines, "")
    table.insert(lines, T(_("二进制文件路径：%1"), self.DAEMON_BINARY))
    table.insert(lines, T(_("API 地址：http://%1:%2"), self.API_HOST, tostring(self.API_PORT)))

    UIManager:show(InfoMessage:new{
        text = table.concat(lines, "\n"),
    })
end

HIDPassthrough.SCAN_POLL_INTERVAL = 2
HIDPassthrough.PAIR_POLL_INTERVAL = 2
HIDPassthrough.SCAN_TIMEOUT_TICKS = 30

local function urlEncode(s)
    if s == nil then return "" end
    return (tostring(s):gsub("[^%w%-_.~]", function(c)
        return string.format("%%%02X", string.byte(c))
    end))
end

function HIDPassthrough:_cancelPolls()
    if self._scan_poll_cb then
        UIManager:unschedule(self._scan_poll_cb)
        self._scan_poll_cb = nil
    end
    if self._pair_poll_cb then
        UIManager:unschedule(self._pair_poll_cb)
        self._pair_poll_cb = nil
    end
end

local function infoToast(text, is_error)
    UIManager:show(InfoMessage:new{
        text = text,
        timeout = is_error and 4 or 2,
    })
end

local function deviceLabel(dev)
    local name = dev.name
    if name == nil or name == "" then name = dev.address or "?" end
    local proto = dev.protocol
    if proto and proto ~= "" then
        return name .. "  (" .. proto:upper() .. ")"
    end
    return name
end

local function setMenuItems(menu, items, title)
    menu:switchItemTable(title, items, 1)
end

function HIDPassthrough:scanForDevices()
    if not self:isRunning() then
        infoToast(_("守护进程未运行，请先启动守护进程。"), true)
        return
    end
    self:_cancelPolls()

    local menu
    menu = Menu:new{
        title = _("正在扫描…"),
        item_table = {{ text = _("正在扫描…（暂未发现设备）"), dim = true }},
        width = Screen:getWidth(),
        height = Screen:getHeight(),
        is_popout = false,
        onClose = function()
            self:_cancelPolls()
            UIManager:close(menu)
            self:_httpGet("/scan-stop")
        end,
    }
    self._scan_menu = menu
    UIManager:show(menu)

    local body, err = self:_httpGet("/scan")
    if not body then
        UIManager:close(menu)
        infoToast(T(_("扫描失败：%1"), tostring(err)), true)
        return
    end
    self:_pollScan(0)
end

function HIDPassthrough:_pollScan(tick)
    self._scan_poll_cb = function() self:_doPollScan(tick) end
    UIManager:scheduleIn(self.SCAN_POLL_INTERVAL, self._scan_poll_cb)
end

function HIDPassthrough:_doPollScan(tick)
    self._scan_poll_cb = nil
    if not self._scan_menu then return end

    local data, err = self:_httpGetJson("/scan-status")
    if not data then
        UIManager:close(self._scan_menu)
        self._scan_menu = nil
        infoToast(T(_("扫描出错：%1"), tostring(err)), true)
        return
    end

    local devices = data.devices or {}
    if data.scanning then
        if #devices > 0 then
            setMenuItems(self._scan_menu, self:_buildScanItems(devices),
                T(_("正在扫描… (%1)"), tostring(#devices)))
        end
        if tick >= self.SCAN_TIMEOUT_TICKS then
            self:_httpGet("/scan-stop")
        end
        self:_pollScan(tick + 1)
        return
    end

    if data.ok and #devices > 0 then
        setMenuItems(self._scan_menu, self:_buildScanItems(devices),
            T(_("扫描结果 (%1)"), tostring(#devices)))
    else
        UIManager:close(self._scan_menu)
        self._scan_menu = nil
        if data.error then
            infoToast(T(_("扫描失败：%1"), data.error), true)
        else
            infoToast(_("未找到 HID 设备"))
        end
    end
end

function HIDPassthrough:_buildScanItems(devices)
    local items = {}
    for _, dev in ipairs(devices) do
        local addr = dev.address
        local proto = dev.protocol or "ble"
        local name = dev.name or ""
        table.insert(items, {
            text = deviceLabel(dev),
            callback = function()
                if self._scan_menu then
                    UIManager:close(self._scan_menu)
                    self._scan_menu = nil
                end
                self:_cancelPolls()
                self:_httpGet("/scan-stop")
                self:pairDevice(addr, proto, name)
            end,
        })
    end
    return items
end

function HIDPassthrough:pairDevice(addr, protocol, name)
    self:_cancelPolls()

    local msg = InfoMessage:new{
        text = T(_("正在配对 %1…"), addr),
        dismissable = true,
    }
    self._pair_msg = msg
    UIManager:show(msg)

    local url = "/pair?addr=" .. urlEncode(addr)
        .. "&protocol=" .. urlEncode(protocol or "ble")
    if name and name ~= "" then
        url = url .. "&name=" .. urlEncode(name)
    end

    local body, err = self:_httpGet(url)
    if not body then
        UIManager:close(msg)
        self._pair_msg = nil
        infoToast(T(_("配对出错：%1"), tostring(err)), true)
        return
    end
    self:_pollPair(0)
end

function HIDPassthrough:_pollPair(tick)
    self._pair_poll_cb = function() self:_doPollPair(tick) end
    UIManager:scheduleIn(self.PAIR_POLL_INTERVAL, self._pair_poll_cb)
end

function HIDPassthrough:_doPollPair(tick)
    self._pair_poll_cb = nil
    local data, err = self:_httpGetJson("/pair-status")
    if not data then
        if self._pair_msg then UIManager:close(self._pair_msg); self._pair_msg = nil end
        infoToast(T(_("配对出错：%1"), tostring(err)), true)
        return
    end

    if data.pairing then
        if tick > 30 then
            if self._pair_msg then UIManager:close(self._pair_msg); self._pair_msg = nil end
            infoToast(_("配对超时"), true)
            return
        end
        self:_pollPair(tick + 1)
        return
    end

    if self._pair_msg then UIManager:close(self._pair_msg); self._pair_msg = nil end
    if data.ok then
        infoToast(T(_("已成功配对：%1"), data.address or ""))
        self:_afterDeviceAction()
    else
        infoToast(T(_("配对失败：%1"), data.error or _("未知错误")), true)
    end
end

function HIDPassthrough:showPairedDevices()
    local data, err = self:_httpGetJson("/status")
    if not data then
        infoToast(T(_("无法连接守护进程：%1"), tostring(err)), true)
        return
    end
    local devices = data.devices or {}
    if #devices == 0 then
        infoToast(_("暂无已配对设备。请使用“扫描设备”功能添加。"))
        return
    end

    local connected_addr = data.connected_device
    local items = {}
    for _, dev in ipairs(devices) do
        local is_conn = connected_addr
            and dev.address
            and dev.address:upper() == tostring(connected_addr):upper()
        local prefix = is_conn and "● " or "○ "
        local addr  = dev.address
        local proto = dev.protocol or "ble"
        local name  = dev.name or ""
        table.insert(items, {
            text = prefix .. deviceLabel(dev),
            callback = function()
                if self._paired_menu then
                    UIManager:close(self._paired_menu)
                    self._paired_menu = nil
                end
                self:_showDeviceActions(addr, proto, name, is_conn)
            end,
        })
    end

    local menu
    menu = Menu:new{
        title = _("已配对的设备"),
        item_table = items,
        width = Screen:getWidth(),
        height = Screen:getHeight(),
        is_popout = false,
        onClose = function()
            UIManager:close(menu)
            self._paired_menu = nil
        end,
    }
    self._paired_menu = menu
    UIManager:show(menu)
end

function HIDPassthrough:_showDeviceActions(addr, proto, name, is_connected)
    local label = (name and name ~= "" and name) or addr
    local items = {}
    if is_connected then
        table.insert(items, {
            text = _("断开连接"),
            callback = function()
                UIManager:close(self._action_menu)
                self._action_menu = nil
                self:_disconnectDevice(addr)
            end,
        })
    else
        table.insert(items, {
            text = _("连接"),
            callback = function()
                UIManager:close(self._action_menu)
                self._action_menu = nil
                self:_connectDevice(addr, proto)
            end,
        })
    end
    table.insert(items, {
        text = _("移除设备 (取消配对)"),
        callback = function()
            UIManager:close(self._action_menu)
            self._action_menu = nil
            UIManager:show(ConfirmBox:new{
                text = T(_("确定要移除设备 %1 吗？"), addr),
                ok_text = _("移除"),
                ok_callback = function() self:_removeDevice(addr) end,
            })
        end,
    })

    local menu
    menu = Menu:new{
        title = label,
        item_table = items,
        width = Screen:getWidth(),
        height = Screen:getHeight(),
        is_popout = false,
        onClose = function()
            UIManager:close(menu)
            self._action_menu = nil
        end,
    }
    self._action_menu = menu
    UIManager:show(menu)
end

function HIDPassthrough:_afterDeviceAction()
    UIManager:scheduleIn(0.4, function() self:showPairedDevices() end)
end

function HIDPassthrough:_connectDevice(addr, proto)
    infoToast(T(_("正在连接 %1…"), addr))
    UIManager:nextTick(function()
        local url = "/connect?addr=" .. urlEncode(addr)
            .. "&protocol=" .. urlEncode(proto or "ble")
        local data, err = self:_httpGetJson(url)
        if not data then
            infoToast(T(_("连接出错：%1"), tostring(err)), true)
            return
        end
        if data.ok then
            infoToast(_("已发送连接请求"))
        else
            infoToast(T(_("连接失败：%1"), data.error or _("未知错误")), true)
        end
        self:_afterDeviceAction()
    end)
end

function HIDPassthrough:_disconnectDevice(addr)
    infoToast(T(_("正在断开与 %1 的连接…"), addr))
    UIManager:nextTick(function()
        local data, err = self:_httpGetJson("/disconnect?addr=" .. urlEncode(addr))
        if not data then
            infoToast(T(_("断开连接出错：%1"), tostring(err)), true)
            return
        end
        if data.ok then
            infoToast(_("已断开连接"))
        else
            infoToast(T(_("断开连接失败：%1"), data.error or _("未知错误")), true)
        end
        self:_afterDeviceAction()
    end)
end

function HIDPassthrough:_removeDevice(addr)
    infoToast(T(_("正在移除设备 %1…"), addr))
    UIManager:nextTick(function()
        local data, err = self:_httpGetJson("/remove?addr=" .. urlEncode(addr))
        if not data then
            infoToast(T(_("移除设备出错：%1"), tostring(err)), true)
            return
        end
        if data.ok then
            infoToast(_("设备已移除"))
        else
            infoToast(T(_("移除设备失败：%1"), data.error or _("未知错误")), true)
        end
        self:_afterDeviceAction()
    end)
end

HIDPassthrough.LOG_LINES = 100

function HIDPassthrough:showLogs()
    local data, err = self:_httpGetJson("/logs?lines=" .. tostring(self.LOG_LINES))
    local text
    if not data then
        text = T(_("无法获取日志：%1"), tostring(err))
    elseif data.lines and #data.lines > 0 then
        text = table.concat(data.lines, "\n")
    else
        text = _("(无日志记录)")
    end

    local viewer
    viewer = TextViewer:new{
        title = _("最近日志"),
        text = text,
        justified = false,
        buttons_table = {
            {
                {
                    text = _("刷新"),
                    callback = function()
                        UIManager:close(viewer)
                        self:showLogs()
                    end,
                },
                {
                    text = _("关闭"),
                    callback = function() UIManager:close(viewer) end,
                },
            },
        },
    }
    UIManager:show(viewer)
    -- 像 tail 一样直接滚动到最新日志行。
    if viewer.scroll_widget then
        viewer.scroll_widget:scrollToBottom()
    end
end

function HIDPassthrough:clearCache()
    UIManager:show(ConfirmBox:new{
        text = _("确定要清除所有缓存的 HID 描述符吗？"),
        ok_text = _("清除"),
        ok_callback = function()
            UIManager:nextTick(function()
                local data, err = self:_httpGetJson("/clear-cache")
                if not data then
                    infoToast(T(_("清除缓存出错：%1"), tostring(err)), true)
                    return
                end
                if data.ok then
                    local n = data.files_removed
                    if n then
                        infoToast(T(_("缓存已清除 (共 %1 个文件)"), tostring(n)))
                    else
                        infoToast(_("缓存已清除"))
                    end
                else
                    infoToast(T(_("清除缓存失败：%1"),
                        data.error or _("未知错误")), true)
                end
            end)
        end,
    })
end

------------------------------------------------------------------------------
-- 菜单集成
------------------------------------------------------------------------------

function HIDPassthrough:onDispatcherRegisterActions()

    Dispatcher:registerAction("hidpassthrough_start", {
        category = "none",
        event    = "HIDPassthroughStart",
        title    = _("HID Passthrough：启动守护进程"),
        general  = true,
    })
    Dispatcher:registerAction("hidpassthrough_stop", {
        category = "none",
        event    = "HIDPassthroughStop",
        title    = _("HID Passthrough：停止守护进程"),
        general  = true,
    })
    Dispatcher:registerAction("hidpassthrough_toggle", {
        category = "none",
        event    = "HIDPassthroughToggle",
        title    = _("HID Passthrough：切换守护进程状态"),
        general  = true,
    })

    -- 上游原生只提供了“翻页”微调器（Turn pages），此处增加固定单步翻页动作。
    Dispatcher:registerAction("hidpassthrough_next_page", {
        category = "none",
        event    = "GotoViewRel",
        arg      = 1,
        title    = _("下一页"),
        reader   = true,
    })
    Dispatcher:registerAction("hidpassthrough_prev_page", {
        category = "none",
        event    = "GotoViewRel",
        arg      = -1,
        title    = _("上一页"),
        reader   = true,
    })

    Dispatcher:registerAction("hidpassthrough_close", {
        category = "none",
        event    = "HIDPassthroughClose",
        title    = _("关闭菜单或对话框"),
        general  = true,
    })
end

-- 在到达 ReaderUI/FileManager 之前一层停止关闭，因为它们的 onClose 会直接退出书籍。
function HIDPassthrough:onHIDPassthroughClose()
    local target, reached_base
    for widget in UIManager:topdown_widgets_iter() do
        if widget == self.ui then
            reached_base = true
            break
        end
        if not target and not widget.toast and not widget.invisible then
            target = widget
        end
    end
    if not target or not reached_base then return true end

    -- 延迟执行：当前正处于 UIManager 遍历正在修改的栈内部。
    UIManager:nextTick(function()
        if not UIManager:isWidgetShown(target) then return end
        if target.onClose then
            target:onClose()
        else
            UIManager:close(target)
        end
    end)
    return true
end

-- start() 最长可能阻塞 15 秒，因此先弹出提示并在下一帧（next tick）中执行任务。
function HIDPassthrough:_runActionAsync(label, fn)
    UIManager:show(InfoMessage:new{
        text = label,
        timeout = 1,
    })
    UIManager:nextTick(function()
        local ok, msg = fn(self)
        UIManager:show(InfoMessage:new{
            text = msg,
            timeout = ok and 2 or 4,
        })
    end)
end

-- 返回 true，防止 UIManager 将其作为活动控件重复传回给我们。
function HIDPassthrough:onHIDPassthroughStart()
    self:_runActionAsync(_("正在启动 HID Passthrough 守护进程…"), self.start)
    return true
end

function HIDPassthrough:onHIDPassthroughStop()
    self:_runActionAsync(_("正在停止 HID Passthrough 守护进程…"), self.stop)
    return true
end

function HIDPassthrough:onHIDPassthroughToggle()
    local label = self:isRunning()
        and _("正在停止 HID Passthrough 守护进程…")
        or  _("正在启动 HID Passthrough 守护进程…")
    self:_runActionAsync(label, self.toggle)
    return true
end

-- AutoSuspend 是重新激活 powerd t1 超时的唯一机制，但它可能会被关闭 (#136)。
local T1_RESET_INTERVAL = 4 * 60
local last_t1_reset = nil

function HIDPassthrough:onInputEvent()
    if not PowerD.resetT1Timeout or PluginShare.keepalive then return end
    if PowerD:isCharging() and not PowerD:isCharged() then return end

    local now = UIManager:getElapsedTimeSinceBoot()
    if last_t1_reset and time.to_number(now - last_t1_reset) < T1_RESET_INTERVAL then
        return
    end
    last_t1_reset = now
    logger.dbg("HIDPassthrough: 已重新激活 powerd 的 t1 超时限制")
    PowerD:resetT1Timeout()
end

function HIDPassthrough:init()
    self:onDispatcherRegisterActions()
    self.ui.menu:registerToMainMenu(self)
    self:_extendEventMap()
    self:registerKeyEvents()
    -- 避免当菜单覆盖我们时无法触发按键绑定。使用与 Screenshoter 相同的技巧。
    if self.ui.active_widgets then
        table.insert(self.ui.active_widgets, self)
    end
    UIManager.event_hook:registerWidget("InputEvent", self)
    -- 设备可能已经处于连接状态。
    self:_scanInputs()
    -- 持续重新挂载被重建的 UHID 节点、重新应用事件映射，
    -- 使守护进程重连或 externalkeyboard 替换映射表都无法让按键失效。
    self:_scheduleRescan()
end

function HIDPassthrough:onCloseWidget()
    self._closing = true
    self:_cancelPolls()
end

function HIDPassthrough:_doToggle(touchmenu_instance)
    local ok, msg = self:toggle()
    UIManager:show(InfoMessage:new{
        text = msg,
        timeout = ok and 2 or 4,
    })
    if touchmenu_instance then
        touchmenu_instance:updateItems()
    end
end

function HIDPassthrough:addToMainMenu(menu_items)
    menu_items.hid_passthrough = {
        text = _("蓝牙管理 - HID Passthrough"),
        -- 归类于 设置 → 网络（与 SSH 放在一起）。
        sorting_hint = "network",
        -- 顶层勾选状态镜像反映守护进程运行状态，
        -- 方便用户在“网络”菜单中一目了然地确认其是否已启动。
        checked_func = function() return self:isRunning() end,
        -- 长按父菜单项可在不进入子菜单的情况下直接切换状态。
        hold_callback = function(touchmenu_instance)
            self:_doToggle(touchmenu_instance)
        end,
        sub_item_table = {
            {
                text = _("HID Passthrough 守护进程"),
                checked_func = function() return self:isRunning() end,
                check_callback_updates_menu = true,
                callback = function(touchmenu_instance)
                    self:_doToggle(touchmenu_instance)
                end,
            },
            {
                text = _("扫描设备"),
                enabled_func = function() return self:isRunning() end,
                keep_menu_open = true,
                callback = function() self:scanForDevices() end,
                separator = true,
            },
            {
                text = _("已配对的设备"),
                enabled_func = function() return self:isRunning() end,
                keep_menu_open = true,
                callback = function() self:showPairedDevices() end,
            },
            {
                text = _("按键映射"),
                keep_menu_open = true,
                sub_item_table_func = function() return self:genKeymapMenu() end,
                separator = true,
            },
            {
                text = _("调试"),
                separator = true,
                sub_item_table = {
                    {
                        text = _("显示守护进程状态"),
                        keep_menu_open = true,
                        callback = function() self:showInfo() end,
                    },
                    {
                        text = _("最近日志"),
                        keep_menu_open = true,
                        callback = function() self:showLogs() end,
                    },
                    {
                        text = _("输入设备诊断信息"),
                        keep_menu_open = true,
                        callback = function() self:showInputDiagnostics() end,
                    },
                    {
                        text = _("清除描述符缓存"),
                        enabled_func = function() return self:isRunning() end,
                        keep_menu_open = true,
                        callback = function() self:clearCache() end,
                    },
                },
            },
            {
                text = _("关于 HID Passthrough"),
                keep_menu_open = true,
                callback = function()
                    UIManager:show(InfoMessage:new{
                        text = T(_([[用于管理 kindle-hid-passthrough 蓝牙 HID 守护进程。

二进制文件路径：%1
API 地址：       http://%2:%3

使用前守护进程必须已预先安装在设备上。详见：
https://github.com/zampierilucas/kindle-hid-passthrough]]),
                            self.DAEMON_BINARY,
                            self.API_HOST,
                            tostring(self.API_PORT)),
                    })
                end,
            },
        },
    }
end

return HIDPassthrough
