import syslog

from constants import *

class Logger:
    def __init__(self, ident=None, facility=None):
        self.ident = ident or ""
        self.facility = facility or syslog.LOG_LOCAL0
        syslog.openlog(
            ident=self.ident,
            facility=self.facility
        )
    def debug(self, message):
        syslog.syslog(syslog.LOG_DEBUG, "[DEBUG] " + str(message))
    def info(self, message):
        syslog.syslog(syslog.LOG_INFO, "[INFO] " + str(message))
    def warning(self, message):
        syslog.syslog(syslog.LOG_WARNING, "[WARNING] " + str(message))
    def error(self, message):
        syslog.syslog(syslog.LOG_ERR, "[ERROR] " + str(message))
    def alarm(self, message):
        syslog.syslog(syslog.LOG_ALERT, "[ALARM] " + str(message))


log = Logger(LOG_IDENT or "", facility=(LOG_FACILITY or syslog.LOG_LOCAL6))
log.warning("Logger started")

