import bottle
import requests
import json
import traceback

from constants import *
from backend.logger import log
import models.message

bottle.TEMPLATE_PATH.insert(0, API_ROOT + "views/")

## website stuff

@bottle.get(URL_ROOT + "test")
def itworks():
    return "I'm the MMS gateway API, and your browser access works!"

#@bottle.get("/v1/<agent>/auth")
#@auth_agent
#def confirm_auth(agent):
#    return "I'm the MMS gateway API, and your agent {} is authorized!".format(agent)

@bottle.get(URL_ROOT + "covfefe.png")
def serve_covfefe():
    return static_file("covfefe.png", root="")


if __name__ == '__main__':
    bottle.run(host="0.0.0.0", port=API_DEV_PORT, reloader=True)
else:
    app = application = bottle.default_app()

