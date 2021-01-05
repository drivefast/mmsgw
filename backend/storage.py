import redis
import rq

from constants import *
from backend.logger import log

rdb = redis.Redis(
    host=STORAGE_CONN.get('host', "localhost"),
    port=STORAGE_CONN.get('port', 6379),
    db=STORAGE_CONN.get('db', 0),
    decode_responses=True
)
try:
    rdb.set('storage', "blah")
    if rdb.get('storage') == "blah":
        rdb.delete('storage')
    else:
        raise Exception('Redis message storage didnt get what it set')
except Exception as err:
    log.alarm("Failed to connect or use redis message storage {}:{}/{} - {}".format(
        STORAGE_CONN.get('host', "localhost"), STORAGE_CONN.get('port', 6379), STORAGE_CONN.get('db', 0), 
        str(err)
    ))
    rdb = None

rdbq = redis.Redis(
    host=QUEUE_CONN.get('host', "localhost"),
    port=QUEUE_CONN.get('port', 6379),
    db=QUEUE_CONN.get('db', 0)
)
try:
    rdbq.set('queues', "blah")
    if rdbq.get('queues').decode() == "blah":
        rdbq.delete('queues')
    else:
        raise Exception('Queue storage didnt get what it set')
except Exception as err:
    log.alarm("Failed to connect or use redis message storage {}:{}/{} - {}".format(
        QUEUE_CONN.get('host', "localhost"), QUEUE_CONN.get('port', 6379), QUEUE_CONN.get('db', 0), 
        str(err)
    ))
    rdbq = None

