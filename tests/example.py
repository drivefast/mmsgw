import bottle
import requests

from constants import *
from backend.logger import log

# this is the endpoint where your application receives transaction-related events
# the events_url setting in the gateway configuration would be handled here
@bottle.post(URL_ROOT + "example/mmsevent")
def mmsevent():
    ev = bottle.request.json
    log.debug(">>>> example application received new message event: {}".format(ev))
    return {'type': "event"}


# this is the endpoint where your application receives notifications for incoming MOs
# the mms_received_url setting in the gateway configuration would be handled here
@bottle.post(URL_ROOT + "example/mms_received")
def mmsmo():
    m = bottle.request.json
    log.debug(">>>> example application received new message: {}".format(m))
    log.debug(">>>> id {} gateway '{}' from {} to {}: {}".format(
        m['template']['id'], m['gateway'],
        m['template']['origin'], m['destination'], 
        m['template']['subject'] or "(no subject)"
    ))
    log.debug(">>>> parts:")
    for p in m['template']['parts']:
        if p['content']:
            content = p['content']
        elif p['content_url']:
            content = "at " + p['content_url']
        log.debug(">>>>>>>> {} ({}): {}".format(p['content_name'], p['content_type'], content))

    # TODO: you need to set this to be the URL of your MMS gateway, that's  where you send 
    # MO processing events
    MMSGW_URL = "https://api.mmsgw.org/mmsgw/v1/"

#    if m['ack_requested']:
    if True:
        # we need to call the gateway and have it send an ACK for the received message 
        # to its peer

        # TODO: make your decision on whether you accept or reject the incoming MO, based on 
        #     whatever criteria you cere for: destinaton numbers, message content and size, etc
        # your acknowledgement does not have to be synchronous - you may queue it as a task
        #     and send it at a later time

        # once you decided on the destinations that are acceptable and the ones that are not,
        # send an http request back to the gateway to ack or send errors accordingly
        # use the applies_to parameter to bundle up destinations that the ACK/nACK commonly 
        # applies to, or dont provide it at all if the status applies to all
        rp = requests.post(MMSGW_URL + "mms/inbound/ack/" + m['id'], json={
            "gateway": m['gateway'],     # the gateway that this message needs to be sent thru
            "message": m['id'], # our own message ID
            "event_for": m['ack_at_addr'], # the address to send the event to
            "provider_ref": m['provider_ref'], # provider's original id (X-Mms-Message-Id)
            "status": "200", # canonical status id
            "description": "Ok", # verbose description of the status
            #"applies_to": [] # phone number(s) this status applies to; missing means applies to all
        })
        log.debug(">>>> POST request status {}".format(rp.status_code))
        # TODO: add error handling

    if m['dr_requested']:

        # TODO:for each number on the destination list, inform the gateway whether the delivery 
        #     was successful or it failed, so that this is eventually reported to the sender
        # your delivery report doesn't have to be sent immediately - you may queue and send 
        #     at a later time
        
        dr_for_numbers = [ "18005551212", "18005551234" ]
        status = "200"; description = "Ok"
        requests.post(MMSGW_URL + "mms/inbound/dr/" + m['id'], json={
            "gateway": m['gateway'],
            "message": m['id'], 
            "provider_ref": m['provider_ref'],
            "status": status, 
            "description": description, 
            "applies_to": dr_for_numbers,
        })
        # TODO: add error handling


    if m['rr_requested']:

        # TODO:for each number on the destination list, tell the gateway when you detected that 
        #     the content of the message got read by the mobile user (read-report)
        # your read report doesn't have to be sent immediately - you may queue and send later

        # invoke the /mms/inbound/rr/<message_id> URL of the gateway similar with above 
        pass

    return {'type': "mo"}

