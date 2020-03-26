import bottle

from constants import *

import models.template
if ENABLE_TESTS:
    import tests.basic
    import tests.example


if __name__ == '__main__':
    bottle.run(host="0.0.0.0", port=API_DEV_PORT, reloader=True)
else:
    app = application = bottle.default_app()

