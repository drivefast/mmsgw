import requests
from rq import Connection, Worker

from constants import *
from backend.logger import log
from backend.storage import rdbq
import backend.util

with Connection(connection=rdbq):
    w = Worker([ 'QMO', 'QEV' ])
    w.work()


