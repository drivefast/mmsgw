import os
import sys
import shutil
import asyncore
import uuid
import rq
import configparser
import pyinotify
from smtpd import SMTPServer
import email, email.utils

import traceback

from backend.logger import log
from backend.storage import rdbq


def dispatch(content, sender, receivers, source=None):

    log.info(">>>> {} inbound on MM4 interface - From: {}, To: {}"
        .format(source or "", sender, receivers)
    )

    # get a gateway that can handle the message; preference is to search 
    # by receiver address first, by sending host next, and by sender address last
    gw = \
        cfg['receivers'].get(email.utils.parseaddr(receivers[0])[1]) or \
        cfg['peers'].get(source[0]) if source is not None else None or \
        cfg['senders'].get(email.utils.parseaddr(sender)[1])
    if gw is None:
        log.warning(">>>> no gateway to process this email")
        return "555 MAIL FROM/RCPT TO parameters not recognized"

    mm4rx_id = str(uuid.uuid4()).replace("-", "")
    
    # move content as file to be processed
    fn = cfg['general']['mail_repo'] + mm4rx_id + ".mm4"
    if cfg['general'].get('smtp_host'):
        with open(fn, "w") as fh:
            fh.write(content)

    # post a task for the gateway parser
    q_rx = rq.Queue("QRX-" + gw, connection=rdbq)
    q_rx.enqueue_call(
        func='models.gateway.mm4rx', args=( fn, ),
        job_id=mm4rx_id,
        ttl=30
    )
    log.info(">>>> message {}, queued for processing by gateway {}".format(mm4rx_id, gw))

    return None


class MaildirEventHandler(pyinotify.ProcessEvent):

    spool_dir = None

    def process_IN_CLOSE_WRITE(self, ev):
        self._process(ev)
    def process_IN_MOVED_TO(self, ev):
        self._process(ev)

    def _process(self, ev):
        # change name of the file asap, to minimize the probability of racing conditions 
        # when processing across multiple instances of the app
        if ev.name.startswith("_"): return
        fn = self.spool_dir + "_" + ev.name
        try:
            shutil.move(ev.pathname, fn)
        except Exception as e:
            log.warning(">>>> possible MM4 file watcher racing condition: " + str(e))
            return
        # parse the file content to get the from and to addresses
        try:
            with open(fn, "r") as fh:
                raw_msg = fh.read()
                msg = email.message_from_string(raw_msg)
                dispatch(raw_msg,
                    msg.get('from'), 
                    email.utils.getaddresses(msg.get_all('to')) 
                )
        except email.errors.MessageParseError as me:
            log.warning(">>>> MM4 file watcher failed to parse {}: {}"
                .format(spool_fn, me)
            )
        except Exception as e:
            log.debug(traceback.format_exc())
            log.warning(">>>> MM4 file watcher failed: {}".format(e))


class MM4SMTPServer(SMTPServer):
    def process_message(self, sender_host, from_addr, to_addr, data):
        return dispatch(data, from_addr, to_addr, sender_host)


if len(sys.argv) < 2:
    print("To start the MM4 mail utility, use a configuration filename as a command line argument.\n")
    exit()
cfg = configparser.ConfigParser()
cfg.read(sys.argv[len(sys.argv) - 1])

bind_host = cfg['general'].get('smtp_host', '')
bind_port = int(cfg['general'].get('smtp_port', 25))
if bind_host:
    _1 = MM4SMTPServer(( bind_host, bind_port ), None)
    log.warning(">>>> MM4 SMTP daemon started, listening on {}:{}".format(bind_host, bind_port))

spool = cfg['general'].get('spool_dir')
if spool:
    wm = pyinotify.WatchManager()
    h = MaildirEventHandler()
    h.spool_dir = spool
    notifier = pyinotify.AsyncNotifier(wm, h)
    _2 = wm.add_watch(spool, 
        pyinotify.IN_CLOSE_WRITE | pyinotify.IN_MOVED_TO
#        exclude_filter=pyinotify.ExcludeFilter([ spool + "_*" ])
    )
    log.warning(">>>> MM4 file daemon started, watching " + spool)

asyncore.loop()

