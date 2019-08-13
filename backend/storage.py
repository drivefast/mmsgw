import redis
import rq

from constants import *
from backend.logger import log

rdb = redis.StrictRedis(
    host=STORAGE_CONN.get('host'),
    port=STORAGE_CONN.get('port', 6379),
    db=STORAGE_CONN.get('db', 0)
)
try:
    rdb.set('storage', "blah")
    if rdb.get('storage') == "blah":
        rdb.delete('storage')
    else:
        raise Exception('Redis message storage didnt get what it set')
except Exception as err:
    log.alarm("Failed to connect or use redis message storage with parameters {}: {}"
        .format(STORAGE_CONN, str(err))
    )
    rdb = None

rdbq = redis.StrictRedis(
    host=QUEUE_CONN.get('host'),
    port=QUEUE_CONN.get('port', 6379),
    db=QUEUE_CONN.get('db', 0)
)
try:
    rdbq.set('queues', "blah")
    if rdbq.get('queues') == "blah":
        rdbq.delete('queues')
    else:
        raise Exception('Queue storage didnt get what it set')
except Exception as err:
    log.alarm("Failed to connect or use queue storage with parameters {}: {}"
        .format(QUEUE_CONN, str(err))
    )
    rdbq = None

