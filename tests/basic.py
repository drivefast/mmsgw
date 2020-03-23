import bottle
import requests
import traceback

from constants import *
from backend.logger import log


## website stuff

bottle.TEMPLATE_PATH.insert(0, API_ROOT + "views/")

@bottle.get(URL_ROOT + "test")
def itworks():
    return "I'm the MMS gateway API, and your browser access works!"

@bottle.get(URL_ROOT + "covfefe.png")
def serve_covfefe():
    return static_file("covfefe.png", root="")

@bottle.get(URL_ROOT + "media/<id>")
def serve_media(media_id):
    return static_file("covfefe.png", root="")


