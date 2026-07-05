/*
 * User-space I/O driver support for HID subsystem
 * Copyright (c) 2012 David Herrmann
 */

/*
 * This program is free software; you can redistribute it and/or modify it
 * under the terms of the GNU General Public License as published by the Free
 * Software Foundation; either version 2 of the License, or (at your option)
 * any later version.
 */

/*
 * Backport of drivers/hid/uhid.c to the 3.0.35-lab126 HID core (Kindle
 * Oasis 1 / duet). The mainline driver targets the post-3.15 hid_ll_driver
 * (.raw_request/.output_report) which does not exist here, so the
 * host->device report paths (OUTPUT, GET_REPORT, SET_REPORT) are dropped.
 * The retained ABI (UHID_CREATE2 / UHID_INPUT2 / UHID_DESTROY, plus the
 * legacy CREATE/INPUT) is the subset the kindle-hid-passthrough daemon uses.
 */

#include <linux/atomic.h>
#include <linux/device.h>
#include <linux/fs.h>
#include <linux/hid.h>
#include <linux/input.h>
#include <linux/miscdevice.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/poll.h>
#include <linux/sched.h>
#include <linux/spinlock.h>
#include <linux/uaccess.h>
#include <linux/uhid.h>
#include <linux/wait.h>

#define UHID_NAME	"uhid"
#define UHID_BUFSIZE	32

/* Not defined in the 3.0.35 miscdevice.h; matches mainline's fixed minor. */
#ifndef UHID_MINOR
#define UHID_MINOR	239
#endif

struct uhid_device {
	struct mutex devlock;
	bool running;

	__u8 *rd_data;
	uint rd_size;

	struct hid_device *hid;
	struct uhid_event input_buf;

	wait_queue_head_t waitq;
	spinlock_t qlock;
	__u8 head;
	__u8 tail;
	struct uhid_event *outq[UHID_BUFSIZE];
};

static struct miscdevice uhid_misc;

static void uhid_queue(struct uhid_device *uhid, struct uhid_event *ev)
{
	__u8 newhead;

	newhead = (uhid->head + 1) % UHID_BUFSIZE;

	if (newhead != uhid->tail) {
		uhid->outq[uhid->head] = ev;
		uhid->head = newhead;
		wake_up_interruptible(&uhid->waitq);
	} else {
		hid_warn(uhid->hid, "Output queue is full\n");
		kfree(ev);
	}
}

static int uhid_queue_event(struct uhid_device *uhid, __u32 event)
{
	unsigned long flags;
	struct uhid_event *ev;

	ev = kzalloc(sizeof(*ev), GFP_KERNEL);
	if (!ev)
		return -ENOMEM;

	ev->type = event;

	spin_lock_irqsave(&uhid->qlock, flags);
	uhid_queue(uhid, ev);
	spin_unlock_irqrestore(&uhid->qlock, flags);

	return 0;
}

static int uhid_hid_start(struct hid_device *hid)
{
	struct uhid_device *uhid = hid->driver_data;
	struct uhid_event *ev;
	unsigned long flags;

	ev = kzalloc(sizeof(*ev), GFP_KERNEL);
	if (!ev)
		return -ENOMEM;

	ev->type = UHID_START;

	if (hid->report_enum[HID_FEATURE_REPORT].numbered)
		ev->u.start.dev_flags |= UHID_DEV_NUMBERED_FEATURE_REPORTS;
	if (hid->report_enum[HID_OUTPUT_REPORT].numbered)
		ev->u.start.dev_flags |= UHID_DEV_NUMBERED_OUTPUT_REPORTS;
	if (hid->report_enum[HID_INPUT_REPORT].numbered)
		ev->u.start.dev_flags |= UHID_DEV_NUMBERED_INPUT_REPORTS;

	spin_lock_irqsave(&uhid->qlock, flags);
	uhid_queue(uhid, ev);
	spin_unlock_irqrestore(&uhid->qlock, flags);

	return 0;
}

static void uhid_hid_stop(struct hid_device *hid)
{
	struct uhid_device *uhid = hid->driver_data;

	hid->claimed = 0;
	uhid_queue_event(uhid, UHID_STOP);
}

static int uhid_hid_open(struct hid_device *hid)
{
	struct uhid_device *uhid = hid->driver_data;

	return uhid_queue_event(uhid, UHID_OPEN);
}

static void uhid_hid_close(struct hid_device *hid)
{
	struct uhid_device *uhid = hid->driver_data;

	uhid_queue_event(uhid, UHID_CLOSE);
}

static int uhid_hid_parse(struct hid_device *hid)
{
	struct uhid_device *uhid = hid->driver_data;

	return hid_parse_report(hid, uhid->rd_data, uhid->rd_size);
}

static struct hid_ll_driver uhid_hid_driver = {
	.start = uhid_hid_start,
	.stop = uhid_hid_stop,
	.open = uhid_hid_open,
	.close = uhid_hid_close,
	.parse = uhid_hid_parse,
};

/* 3.0.35 has no transport-independent generic HID driver (usbhid/hidp each
 * register their own), so register one here to bind uhid-created devices and
 * run hidinput_connect. hid_match_one_id needs an exact bus, so list each. */
static const struct hid_device_id uhid_hid_table[] = {
	{ HID_BLUETOOTH_DEVICE(HID_ANY_ID, HID_ANY_ID) },
	{ HID_USB_DEVICE(HID_ANY_ID, HID_ANY_ID) },
	{ HID_DEVICE(BUS_VIRTUAL, HID_ANY_ID, HID_ANY_ID) },
	{ }
};
MODULE_DEVICE_TABLE(hid, uhid_hid_table);

static struct hid_driver uhid_hid_generic = {
	.name = "generic-uhid",
	.id_table = uhid_hid_table,
};

static int uhid_event_from_user(const char __user *buffer, size_t len,
				struct uhid_event *event)
{
	if (copy_from_user(event, buffer, min(len, sizeof(*event))))
		return -EFAULT;

	return 0;
}

static int uhid_dev_create2(struct uhid_device *uhid,
			    const struct uhid_event *ev)
{
	struct hid_device *hid;
	size_t rd_size, len;
	void *rd_data;
	int ret;

	if (uhid->running)
		return -EALREADY;

	rd_size = ev->u.create2.rd_size;
	if (rd_size <= 0 || rd_size > HID_MAX_DESCRIPTOR_SIZE)
		return -EINVAL;

	rd_data = kmemdup(ev->u.create2.rd_data, rd_size, GFP_KERNEL);
	if (!rd_data)
		return -ENOMEM;

	uhid->rd_size = rd_size;
	uhid->rd_data = rd_data;

	hid = hid_allocate_device();
	if (IS_ERR(hid)) {
		ret = PTR_ERR(hid);
		goto err_free;
	}

	len = min(sizeof(hid->name), sizeof(ev->u.create2.name)) - 1;
	strncpy(hid->name, ev->u.create2.name, len);
	len = min(sizeof(hid->phys), sizeof(ev->u.create2.phys)) - 1;
	strncpy(hid->phys, ev->u.create2.phys, len);
	len = min(sizeof(hid->uniq), sizeof(ev->u.create2.uniq)) - 1;
	strncpy(hid->uniq, ev->u.create2.uniq, len);

	hid->ll_driver = &uhid_hid_driver;
	hid->bus = ev->u.create2.bus;
	hid->vendor = ev->u.create2.vendor;
	hid->product = ev->u.create2.product;
	hid->version = ev->u.create2.version;
	hid->country = ev->u.create2.country;
	hid->driver_data = uhid;
	hid->dev.parent = uhid_misc.this_device;

	uhid->hid = hid;
	uhid->running = true;

	ret = hid_add_device(hid);
	if (ret) {
		hid_err(hid, "Cannot register HID device\n");
		goto err_hid;
	}

	return 0;

err_hid:
	hid_destroy_device(hid);
	uhid->hid = NULL;
	uhid->running = false;
err_free:
	kfree(uhid->rd_data);
	uhid->rd_data = NULL;
	uhid->rd_size = 0;
	return ret;
}

static int uhid_dev_create(struct uhid_device *uhid,
			   struct uhid_event *ev)
{
	struct uhid_create_req orig;

	orig = ev->u.create;

	if (orig.rd_size <= 0 || orig.rd_size > HID_MAX_DESCRIPTOR_SIZE)
		return -EINVAL;
	if (copy_from_user(&ev->u.create2.rd_data, orig.rd_data, orig.rd_size))
		return -EFAULT;

	memcpy(ev->u.create2.name, orig.name, sizeof(orig.name));
	memcpy(ev->u.create2.phys, orig.phys, sizeof(orig.phys));
	memcpy(ev->u.create2.uniq, orig.uniq, sizeof(orig.uniq));
	ev->u.create2.rd_size = orig.rd_size;
	ev->u.create2.bus = orig.bus;
	ev->u.create2.vendor = orig.vendor;
	ev->u.create2.product = orig.product;
	ev->u.create2.version = orig.version;
	ev->u.create2.country = orig.country;

	return uhid_dev_create2(uhid, ev);
}

static int uhid_dev_destroy(struct uhid_device *uhid)
{
	if (!uhid->running)
		return -EINVAL;

	uhid->running = false;

	hid_destroy_device(uhid->hid);
	kfree(uhid->rd_data);

	return 0;
}

static int uhid_dev_input(struct uhid_device *uhid, struct uhid_event *ev)
{
	if (!uhid->running)
		return -EINVAL;

	hid_input_report(uhid->hid, HID_INPUT_REPORT, ev->u.input.data,
			 min_t(size_t, ev->u.input.size, UHID_DATA_MAX), 0);

	return 0;
}

static int uhid_dev_input2(struct uhid_device *uhid, struct uhid_event *ev)
{
	if (!uhid->running)
		return -EINVAL;

	hid_input_report(uhid->hid, HID_INPUT_REPORT, ev->u.input2.data,
			 min_t(size_t, ev->u.input2.size, UHID_DATA_MAX), 0);

	return 0;
}

static int uhid_char_open(struct inode *inode, struct file *file)
{
	struct uhid_device *uhid;

	uhid = kzalloc(sizeof(*uhid), GFP_KERNEL);
	if (!uhid)
		return -ENOMEM;

	mutex_init(&uhid->devlock);
	spin_lock_init(&uhid->qlock);
	init_waitqueue_head(&uhid->waitq);
	uhid->running = false;

	file->private_data = uhid;
	nonseekable_open(inode, file);

	return 0;
}

static int uhid_char_release(struct inode *inode, struct file *file)
{
	struct uhid_device *uhid = file->private_data;
	unsigned int i;

	uhid_dev_destroy(uhid);

	for (i = 0; i < UHID_BUFSIZE; ++i)
		kfree(uhid->outq[i]);

	kfree(uhid);

	return 0;
}

static ssize_t uhid_char_read(struct file *file, char __user *buffer,
				size_t count, loff_t *ppos)
{
	struct uhid_device *uhid = file->private_data;
	int ret;
	unsigned long flags;
	size_t len;

	/* they need at least the "type" member of uhid_event */
	if (count < sizeof(__u32))
		return -EINVAL;

try_again:
	if (file->f_flags & O_NONBLOCK) {
		if (uhid->head == uhid->tail)
			return -EAGAIN;
	} else {
		ret = wait_event_interruptible(uhid->waitq,
						uhid->head != uhid->tail);
		if (ret)
			return ret;
	}

	ret = mutex_lock_interruptible(&uhid->devlock);
	if (ret)
		return ret;

	if (uhid->head == uhid->tail) {
		mutex_unlock(&uhid->devlock);
		goto try_again;
	} else {
		len = min(count, sizeof(**uhid->outq));
		if (copy_to_user(buffer, uhid->outq[uhid->tail], len)) {
			ret = -EFAULT;
		} else {
			kfree(uhid->outq[uhid->tail]);
			uhid->outq[uhid->tail] = NULL;

			spin_lock_irqsave(&uhid->qlock, flags);
			uhid->tail = (uhid->tail + 1) % UHID_BUFSIZE;
			spin_unlock_irqrestore(&uhid->qlock, flags);
		}
	}

	mutex_unlock(&uhid->devlock);
	return ret ? ret : len;
}

static ssize_t uhid_char_write(struct file *file, const char __user *buffer,
				size_t count, loff_t *ppos)
{
	struct uhid_device *uhid = file->private_data;
	int ret;
	size_t len;

	/* we need at least the "type" member of uhid_event */
	if (count < sizeof(__u32))
		return -EINVAL;

	ret = mutex_lock_interruptible(&uhid->devlock);
	if (ret)
		return ret;

	memset(&uhid->input_buf, 0, sizeof(uhid->input_buf));
	len = min(count, sizeof(uhid->input_buf));

	ret = uhid_event_from_user(buffer, len, &uhid->input_buf);
	if (ret)
		goto unlock;

	switch (uhid->input_buf.type) {
	case UHID_CREATE:
		ret = uhid_dev_create(uhid, &uhid->input_buf);
		break;
	case UHID_CREATE2:
		ret = uhid_dev_create2(uhid, &uhid->input_buf);
		break;
	case UHID_DESTROY:
		ret = uhid_dev_destroy(uhid);
		break;
	case UHID_INPUT:
		ret = uhid_dev_input(uhid, &uhid->input_buf);
		break;
	case UHID_INPUT2:
		ret = uhid_dev_input2(uhid, &uhid->input_buf);
		break;
	default:
		ret = -EOPNOTSUPP;
	}

unlock:
	mutex_unlock(&uhid->devlock);

	/* return "count" not "len" to not confuse the caller */
	return ret ? ret : count;
}

static unsigned int uhid_char_poll(struct file *file, poll_table *wait)
{
	struct uhid_device *uhid = file->private_data;

	poll_wait(file, &uhid->waitq, wait);

	if (uhid->head != uhid->tail)
		return POLLIN | POLLRDNORM;

	return 0;
}

static const struct file_operations uhid_fops = {
	.owner		= THIS_MODULE,
	.open		= uhid_char_open,
	.release	= uhid_char_release,
	.read		= uhid_char_read,
	.write		= uhid_char_write,
	.poll		= uhid_char_poll,
	.llseek		= no_llseek,
};

static struct miscdevice uhid_misc = {
	.fops		= &uhid_fops,
	.minor		= UHID_MINOR,
	.name		= UHID_NAME,
};

static int __init uhid_init(void)
{
	int ret;

	ret = misc_register(&uhid_misc);
	if (ret)
		return ret;

	ret = hid_register_driver(&uhid_hid_generic);
	if (ret)
		misc_deregister(&uhid_misc);

	return ret;
}

static void __exit uhid_exit(void)
{
	hid_unregister_driver(&uhid_hid_generic);
	misc_deregister(&uhid_misc);
}

module_init(uhid_init);
module_exit(uhid_exit);
MODULE_LICENSE("GPL");
MODULE_AUTHOR("David Herrmann <dh.herrmann@gmail.com>");
MODULE_DESCRIPTION("User-space I/O driver support for HID subsystem");
MODULE_ALIAS_MISCDEV(UHID_MINOR);
MODULE_ALIAS("devname:" UHID_NAME);
