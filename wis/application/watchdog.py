#!/usr/bin/python
# Copyright 2015 Neuhold Markus and Kleinsasser Mario
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import threading
from common import smsgwglobals
from common import database
from common import error
from common.helper import GlobalHelper
from datetime import datetime
from time import sleep
from application import wisglobals
from application.smstransfer import Smstransfer
from application.helper import Helper
from application import apperror
from queue import Queue, Empty
import urllib.request
import json
import socket

class Watchdog_Route(threading.Thread):

    def __init__(self, threadID, name, routingid):
        super(Watchdog_Route, self).__init__()
        if not routingid in wisglobals.watchdogRouteThreadQueue:
            self.queue = Queue()
            wisglobals.watchdogRouteThreadQueue[routingid] = self.queue

        wisglobals.watchdogRouteThread[routingid] = self
        wisglobals.watchdogRouteThreadNotify[routingid] = threading.Event()
        self.e = threading.Event()
        self.threadID = threadID
        self.name = name
        self.routingid = routingid
        self.modemid = threadID

    def run(self):
        smsgwglobals.wislogger.debug("WATCHDOG [route: " + str(self.routingid) + "]: starting")
        while not self.e.isSet():
            smsgwglobals.wislogger.debug("WATCHDOG [route: " + str(self.routingid) + "]: sleep for sms")
            wisglobals.watchdogRouteThreadNotify[self.routingid].wait()
            smsgwglobals.wislogger.debug("WATCHDOG [route: " + str(self.routingid) + "]: running for sms")
            if self.e.is_set():
                continue

            # processing sms in database
            smsgwglobals.wislogger.debug("WATCHDOG [route: " + str(self.routingid) + "]: start sending sms")
            self.process()
            smsgwglobals.wislogger.debug("WATCHDOG [route: " + str(self.routingid) + "]: finished processing sms")

            try:
                wisglobals.watchdogRouteThreadNotify[self.routingid].clear()
                smsgwglobals.wislogger.debug("WATCHDOG [route: " + str(self.routingid) + "]: clear for next sms run")
            except:
                pass

        smsgwglobals.wislogger.debug("WATCHDOG [route: " + str(self.routingid) + "]: stopped")

    def send(self, sms):
        smstrans = sms["sms"]
        route = sms["route"]
        # encode to json
        jdata = json.dumps(smstrans.smsdict, default=str)
        data = GlobalHelper.encodeAES(jdata)

        request = \
            urllib.request.Request(
                route[0]["pisurl"] +
                "/sendsms")

        request.add_header("Content-Type",
                           "application/json;charset=utf-8")

        try:
            smsgwglobals.wislogger.debug("WATCHDOG [route: " + str(self.routingid) + "] " +
                                         "Sending VIA " +
                                         smstrans.smsdict["modemid"] +
                                         route[0]["pisurl"] +
                                         "/sendsms")
            f = urllib.request.urlopen(request, data, timeout=wisglobals.pissendtimeout)
            smsgwglobals.wislogger.debug("WATCHDOG [route: " + str(self.routingid) + "] SMS send to PIS returncode:" + str(f.getcode()))
            # if all is OK set the sms status to SENT
            smstrans.smsdict["statustime"] = datetime.utcnow()
            if f.getcode() == 200:
                status_code = f.read()

                if int(status_code) == 1:
                    if smstrans.smsdict["status"] == 0:
                        smstrans.smsdict["status"] = 1
                        smsgwglobals.wislogger.debug("WATCHDOG [route: " + str(self.routingid) + "] SEND direct:" + str(smstrans.smsdict))
                    else:
                        smstrans.smsdict["status"] = 101
                        smsgwglobals.wislogger.debug("WATCHDOG [route: " + str(self.routingid) + "] SEND deligated:" + str(smstrans.smsdict))
                    smsgwglobals.wislogger.debug("WATCHDOG [route: " + str(self.routingid) + "] SEND Update DB SUCCESS:" + str(smstrans.smsdict))
                    smstrans.updatedb()
                elif int(status_code) == 2000 or int(status_code) == 31 or int(status_code) == 27:
                    # PIS doesn't have modem endpoint - reprocess SMS and choose different route) - Error 2000
                    # Modem fail - reprocess SMS and choose different route) - Error 31 (can't read SMSC nummber, 99.99% - we just lost connection)
                    # Modem fail - reprocess SMS and choose different route) - Error 27 ( no money or SIM card blocked)
                    # BUT use same smsid (after new route will be choosed it will decrease sms_count on route (IMSI)
                    smsgwglobals.wislogger.debug("WATCHDOG [route: " + str(self.routingid) + "] PIS can't reach PID: " + str(smstrans.smsdict))
                    try:
                        Helper.processsms(smstrans)
                    except apperror.NoRoutesFoundError:
                        pass
                    else:
                        # Add sms to global queue
                        wisglobals.watchdogThread.queue.put(smstrans.smsdict["smsid"])
                        wisglobals.watchdogThreadNotify.set()
                else:
                    if smstrans.smsdict["status"] == 0:
                        smstrans.smsdict["status"] = int(status_code)
                        smsgwglobals.wislogger.debug("WATCHDOG [route: " + str(self.routingid) + "] SEND direct ERROR:" + str(smstrans.smsdict))
                    else:
                        smstrans.smsdict["status"] = 100 + int(status_code)
                        smsgwglobals.wislogger.debug("WATCHDOG [route: " + str(self.routingid) + "] SEND deligated ERROR:" + str(smstrans.smsdict))
                    smsgwglobals.wislogger.debug("WATCHDOG [route: " + str(self.routingid) + "] SEND Update DB ERROR:" + str(smstrans.smsdict))
                    smstrans.updatedb()

        except urllib.error.URLError as e:
            if smstrans.smsdict["status"] == 0:
                smstrans.smsdict["status"] = 200
            else:
                smstrans.smsdict["status"] = 300

            smsgwglobals.wislogger.debug("WATCHDOG [route: " + str(self.routingid) + "] SEND EXCEPTION " + str(smstrans.smsdict))
            smstrans.updatedb()
            # set SMS to not send!!!
            smsgwglobals.wislogger.debug(e)
            smsgwglobals.wislogger.debug("WATCHDOG [route: " + str(self.routingid) + "] SEND Get peers NOTOK")

            # On 500 error - (probably PID/route died - try to reprocess sms)
            sleep(5)
            try:
                Helper.processsms(smstrans, reprocess_sms=True)
            except apperror.NoRoutesFoundError:
                pass
            else:
                # Add sms to global queue
                wisglobals.watchdogThread.queue.put(smstrans.smsdict["smsid"])
                wisglobals.watchdogThreadNotify.set()
        except socket.timeout as e:
            smstrans.smsdict["status"] = 400
            smstrans.updatedb()
            smsgwglobals.wislogger.debug(e)
            smsgwglobals.wislogger.debug("WATCHDOG [route: " + str(self.routingid) + "] SEND Socket connection timeout")

            # On 400 error - (probably PID/route died - try to reprocess sms)
            sleep(5)
            try:
                Helper.processsms(smstrans, reprocess_sms=True)
            except apperror.NoRoutesFoundError:
                pass
            else:
                # Add sms to global queue
                wisglobals.watchdogThread.queue.put(smstrans.smsdict["smsid"])
                wisglobals.watchdogThreadNotify.set()

    def process(self):
        smsgwglobals.wislogger.debug("WATCHDOG [route: " + str(self.routingid) + "] processing sms")

        try:
            sms = self.queue.get(block=False)
        except Empty:
            # No sms in queue
            smsgwglobals.wislogger.debug("WATCHDOG [route: " + str(self.routingid) + "] " +
                                         "no SMS to process in the queue")
            pass
        else:
            self.send(sms)
            # Each modem in any case will be sending SMS in sequantal mode. So sleep a bit
            sleep(5)
            # Re run processing to make sure that queue empty
            self.process()

    def stop(self):
        self.e.set()

    def stopped(self):
        return self.e.is_set()

    def terminate(self):
        smsgwglobals.wislogger.debug("WATCHDOG [route: " + str(self.routingid) + "] terminating")
        self.stop()
        wisglobals.watchdogRouteThreadNotify[self.routingid].set()


class Watchdog(threading.Thread):

    def __init__(self, threadID, name, queue):
        super(Watchdog, self).__init__()
        wisglobals.watchdogThread = self
        wisglobals.watchdogThreadNotify = threading.Event()
        self.e = threading.Event()
        self.threadID = threadID
        self.name = name
        self.queue = queue

    def dispatch_sms(self, smstrans, route):
        rid = route[0]["routingid"]
        modemid = route[0]["modemid"]
        if not rid in wisglobals.watchdogRouteThread or not wisglobals.watchdogRouteThread[rid].isAlive() or not rid in wisglobals.watchdogRouteThreadQueue:
            wd = Watchdog_Route(modemid, "Watchdog_Route", rid)
            wd.daemon = True
            wd.start()

        queue = wisglobals.watchdogRouteThreadQueue[rid]
        queue.put({ "sms" : smstrans, "route": route})

        wisglobals.watchdogRouteThreadNotify[rid].set()

    def deligate(self, smstrans, route):
        # encode to json
        jdata = smstrans.getjson()
        data = GlobalHelper.encodeAES(jdata)

        request = \
            urllib.request.Request(
                route[0]["wisurl"] +
                "/smsgateway/api/deligatesms")

        request.add_header("Content-Type",
                           "application/json;charset=utf-8")

        try:
            smsgwglobals.wislogger.debug("WATCHDOG: " +
                                         "Deligate VIA " +
                                         route[0]["wisurl"] +
                                         "/smsgateway/api/deligate")
            f = urllib.request.urlopen(request, data, timeout=wisglobals.pissendtimeout)
            smsgwglobals.wislogger.debug("WATCHDOG: SMS deligate to PIS returncode:" + str(f.getcode()))
            # if all is OK set the sms status to SENT
            smstrans.smsdict["statustime"] = datetime.utcnow()
            if f.getcode() == 200:
                smstrans.smsdict["status"] = 3
                smsgwglobals.wislogger.debug("WATCHDOF: DELIGATE SUCCESS " + str(smstrans.smsdict))
                smsgwglobals.wislogger.debug("WATCHDOD: DELIGATE update  DB SUCCESS:" + str(smstrans.smsdict))
                smstrans.updatedb()
            else:
                smstrans.smsdict["status"] = 103
                smsgwglobals.wislogger.debug("WATCHDOF: DELIGATE ERROR " + str(smstrans.smsdict))
                smsgwglobals.wislogger.debug("WATCHDOD: DELIGATE update DB ERROR: " + str(smstrans.smsdict))
                smstrans.updatedb()
        except urllib.error.URLError as e:
            # set SMS to not send!!!
            smstrans.smsdict["status"] = 103
            smstrans.updatedb()
            smsgwglobals.wislogger.debug(e)
            smsgwglobals.wislogger.debug("WATCHDOG: DELIGATE Get peers NOTOK " + str(smstrans.smsdict))
        except socket.timeout as e:
            smsgwglobals.wislogger.debug(e)
            smsgwglobals.wislogger.debug("WATCHDOG: DELIGATE socket connection timeout " + str(smstrans.smsdict))

    def process(self):
        smsgwglobals.wislogger.debug("WATCHDOG: processing sms")

        try:
            sms_id = self.queue.get(block=False)
        except Empty:
            # No sms in queue
            smsgwglobals.wislogger.debug("WATCHDOG: " +
                                         "no SMS to process in the queue")
            pass
        else:
            try:
                db = database.Database()

                # cleanup old sms
                db.delete_old_sms(wisglobals.cleanupseconds)

                smsen = db.read_sms(smsid=sms_id)
                if not smsen:
                    smsgwglobals.wislogger.debug("WATCHDOG: " +
                                             "no SMS with ID: " + sms_id + " in DB")
                    return
            except error.DatabaseError as e:
                smsgwglobals.wislogger.debug(e.message)

            # we have sms, just process
            sms = smsen[0]
            smsgwglobals.wislogger.debug("WATCHDOG: Process SMS: " + str(sms))

            # create smstrans object for easy handling
            smstrans = Smstransfer(**sms)

            # check if we have routes
            # if we have no routes, set error code and
            # continue with the next sms
            routes = wisglobals.rdb.read_routing()
            if routes is None or len(routes) == 0:
                smstrans.smsdict["statustime"] = datetime.utcnow()
                smstrans.smsdict["status"] = 100
                smsgwglobals.wislogger.debug("WATCHDOG: NO routes to process SMS: " + str(smstrans.smsdict))
                smstrans.updatedb()
                return

            # check if modemid exists in routing
            route = wisglobals.rdb.read_routing(
                smstrans.smsdict["modemid"])
            if route is None or len(route) == 0:
                smsgwglobals.wislogger.debug("WATCHDOG: " +
                                              " ALERT ROUTE LOST")
                # try to reprocess route
                smstrans.smsdict["status"] = 106
                smstrans.updatedb()
                try:
                    Helper.processsms(smstrans)
                except apperror.NoRoutesFoundError:
                    pass
                else:
                    self.queue.put(smstrans.smsdict["smsid"])
            elif route[0]["wisid"] != wisglobals.wisid:
                self.deligate(smstrans, route)
            else:
                # we have a route, this wis is the correct one
                # therefore give the sms to the PIS
                # this is a bad hack to ignore obsolete routes
                # this may lead to an error, fixme
                route[:] = [d for d in route if d['obsolete'] < 13]
                smsgwglobals.wislogger.debug("WATCHDOG: process with route %s ", str(route))
                smsgwglobals.wislogger.debug("WATCHDOG: Sending to PIS %s", str(sms))
                # only continue if route contains data
                if len(route) > 0:
                    self.dispatch_sms(smstrans, route)
                else:
                    # Reprocess
                    smstrans.updatedb()
                    try:
                        Helper.processsms(smstrans)
                    except apperror.NoRoutesFoundError:
                        pass
                    else:
                        self.queue.put(smstrans.smsdict["smsid"])

            # Re run processing to make sure that queue empty
            self.process()


    def run(self):
        smsgwglobals.wislogger.debug("WATCHDOG: starting")
        while not self.e.isSet():
            smsgwglobals.wislogger.debug("WATCHDOG: sleep for sms")
            wisglobals.watchdogThreadNotify.wait()
            smsgwglobals.wislogger.debug("WATCHDOG: running for sms")
            if self.e.is_set():
                continue

            # processing sms in database
            smsgwglobals.wislogger.debug("WATCHDOG: start processing sms")
            self.process()
            smsgwglobals.wislogger.debug("WATCHDOG: finished processing sms")

            wisglobals.watchdogThreadNotify.clear()
            smsgwglobals.wislogger.debug("WATCHDOG: clear for next sms run")

        smsgwglobals.wislogger.debug("WATCHDOG: stopped")

    def stop(self):
        self.e.set()

    def stopped(self):
        return self.e.is_set()

    def terminate(self):
        smsgwglobals.wislogger.debug("WATCHDOG: terminating")
        self.stop()
        wisglobals.watchdogThreadNotify.set()