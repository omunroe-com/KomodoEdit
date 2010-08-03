#!/usr/bin/env python
# ***** BEGIN LICENSE BLOCK *****
# Version: MPL 1.1/GPL 2.0/LGPL 2.1
# 
# The contents of this file are subject to the Mozilla Public License
# Version 1.1 (the "License"); you may not use this file except in
# compliance with the License. You may obtain a copy of the License at
# http://www.mozilla.org/MPL/
# 
# Software distributed under the License is distributed on an "AS IS"
# basis, WITHOUT WARRANTY OF ANY KIND, either express or implied. See the
# License for the specific language governing rights and limitations
# under the License.
# 
# The Original Code is Komodo code.
# 
# The Initial Developer of the Original Code is ActiveState Software Inc.
# Portions created by ActiveState Software Inc are Copyright (C) 2000-2007
# ActiveState Software Inc. All Rights Reserved.
# 
# Contributor(s):
#   ActiveState Software Inc
# 
# Alternatively, the contents of this file may be used under the terms of
# either the GNU General Public License Version 2 or later (the "GPL"), or
# the GNU Lesser General Public License Version 2.1 or later (the "LGPL"),
# in which case the provisions of the GPL or the LGPL are applicable instead
# of those above. If you wish to allow use of your version of this file only
# under the terms of either the GPL or the LGPL, and not to allow others to
# use your version of this file under the terms of the MPL, indicate your
# decision by deleting the provisions above and replace them with the notice
# and other provisions required by the GPL or the LGPL. If you do not delete
# the provisions above, a recipient may use your version of this file under
# the terms of any one of the MPL, the GPL or the LGPL.
# 
# ***** END LICENSE BLOCK *****

"""Komodo-specific nsIObserverService implementation to allow the
notify/observe mechanism to be scoped on an object.

Mozilla's nsIObserverService implementation (@mozilla.org/observer-service;1)
is a global thang. This makes it inconvenient to use to pass notifications
to a specific instance of an object if there are many of them. For example:
to notify a specific Komodo document that its encoding has changed via the
global nsIObserverService would require a global listener that would then
pass the notification onto the document instance. If, however, we have a
koObserverService instance attached to the document we can call
.notifyObservers() on it and only registered observers on that particular
document will be bothered by it.

Note: The suffix "Service" on koObserverService is a misnomer because it is
NOT to be used as a service, there must be one instance per user. I.e.
createInstance() must be used instead of getService().
"""

from xpcom import components, ServerException, COMException, nsError
from xpcom.client import WeakReference
from xpcom.server.enumerator import SimpleEnumerator
from xpcom.server import WrapObject, UnwrapObject

import threading
import logging
log = logging.getLogger('KoObserverService')
#log.setLevel(logging.DEBUG)

# a base class to implement observer services

class KoObserverService:
    _com_interfaces_ = [components.interfaces.koIObserverService,
                        components.interfaces.nsIObserverService,
                        components.interfaces.nsIObserver]
    _reg_clsid_ = "3B7D0418-1533-4F03-A759-896C058A734A"
    _reg_contractid_ = "@activestate.com/koObserverService;1"
    _reg_desc_ = "Komodo Python Observer Service"
    
    def __init__(self):
        self._topics = {}
        self.cv = threading.Condition()
        try:
            osSvc = components.classes["@mozilla.org/observer-service;1"].\
                getService(components.interfaces.nsIObserverService)
            self._nsIObserver = WrapObject(self, components.interfaces.nsIObserver)
            osSvc.addObserver(self._nsIObserver, 'xpcom-shutdown', 1)
        except Exception, e:
            log.exception(e)

    def observe(self, dummy, topic, featureName):
        if topic == "xpcom-shutdown":
            #self._removeDead()
            #if self._topics:
            #    log.warn("observers not removed: %r", (self._topics.keys(),))
            self._topics = {}

    def dump(self, topics=None):
        print
        print "KoObserverService"
        if topics is None:
            topics = self._topics.keys()
        for topic in topics:
            observers = self._getLiveObservers(topic)
            print "  %r (%d observers)" % (topic, len(observers))
        print

    # Returns list of observers that are not dead. Maintains a 1-1 match for
    # the returned observers to the "self._topics[topic]" weak references.
    def _getLiveObservers(self, topic):
        wr_observers = self._topics.get(topic)
        if wr_observers is None:
            return None
        L = []
        if wr_observers:
            for i in range(len(wr_observers)-1, -1, -1):
                wr = wr_observers[i]
                if not callable(wr):
                    L.insert(0, wr)
                else:
                    try:
                        observer = wr()
                        if observer is not None:
                            L.insert(0, observer)
                            continue
                    except Exception:
                        # bug 72807, pyxpcom failure on trunk
                        # This is occurs when a JavaScript observer has not removed
                        # it's observer before it was cleaned up (garbage collected).
                        #log.exception("WeakReference failed for topic: %r, wr: %r", topic, wr)
                        pass
                    # It's dead, remove it.
                    log.debug("Removed a dead observer for topic: %r", topic)
                    wr_observers.pop(i)
            # There are no live observers left, remove the topic itself.
            if not wr_observers:
                self._topics.pop(topic)
        return L

    # void addObserver( in nsIObserver anObserver, in string aTopic, in boolean ownsWeak);
    def _addObserver(self, anObserver, aTopic, ownsWeak):
        wr_observers = self._topics.get(aTopic)
        if wr_observers is None:
            wr_observers = []
            self._topics[aTopic] = wr_observers

        # Ignoring the ownsWeak argument, always try to create a
        # weakreference, see comments in bug 80145.
        try:
            anObserver = WeakReference(anObserver)
        except COMException:
            pass
        wr_observers.append(anObserver)

    def addObserver(self, anObserver, aTopic, ownsWeak):
        if not anObserver:
            raise ServerException(nsError.NS_ERROR_FAILURE, "Invalid Observer")
        self.cv.acquire()
        try:
            self._addObserver(anObserver, aTopic, ownsWeak)
        finally:
            self.cv.release()
    
    # void addObserverForTopics( in nsIObserver anObserver, in array aTopics, in boolean ownsWeak);
    def addObserverForTopics(self, anObserver, aTopics, ownsWeak):
        if not anObserver:
            raise ServerException(nsError.NS_ERROR_FAILURE, "Invalid Observer")
        self.cv.acquire()
        try:
            for aTopic in aTopics:
                self._addObserver(anObserver, aTopic, ownsWeak)
        finally:
            self.cv.release()
    
    def _removeObserver(self, anObserver, aTopic):
        # Get non-weakref'd list of observers so we can compare the observer we
        # got with that list. This list (observers) will be the same size/order
        # as the original (self._topics[aTopic]). Probably need to deal with
        # thread safety here?
        observers = self._getLiveObservers(aTopic)
        if observers is None:
            raise ServerException(nsError.NS_ERROR_FAILURE,"No Observers for Topic %r"%aTopic)
        try:
            idx = observers.index(anObserver)
            wr_observers = self._topics.get(aTopic)
            wr_observers.pop(idx)
            if not wr_observers:
                # Can remove the topic as well.
                self._topics.pop(aTopic)
        except ValueError:
            raise ServerException(nsError.NS_ERROR_FAILURE,"Observer not in topic list %s"%aTopic)

    # void removeObserver( in nsIObserver anObserver, in string aTopic );
    def removeObserver(self, anObserver, aTopic):
        self.cv.acquire()
        try:
            self._removeObserver(anObserver, aTopic)
        finally:
            self.cv.release()

    # void removeObserverForTopics( in nsIObserver anObserver, in array aTopics );
    def removeObserverForTopics(self, anObserver, aTopics):
        self.cv.acquire()
        try:
            for aTopic in aTopics:
                self._removeObserver(anObserver, aTopic)
        finally:
            self.cv.release()
    
    #void notifyObservers( in nsISupports aSubject, 
    #                      in string aTopic, 
    #                      in wstring someData );
    def notifyObservers(self, aSubject, aTopic, someData):
        topic_observers = None
        catchall_observers = None

        self.cv.acquire()
        try:
            if aTopic:
                topic_observers = self._getLiveObservers(aTopic)
            # A twist, the empty topic is global and recieves all notifications!
            catchall_observers = self._getLiveObservers('')
        finally:
            self.cv.release()

        if topic_observers:
            for observer in topic_observers:
                try:
                    observer.observe(aSubject, aTopic, someData)
                except:
                    log.exception("notifyObservers:: topic: %r, data: %r", aTopic, someData)

        if catchall_observers:
            for observer in catchall_observers:
                try:
                    observer.observe(aSubject, aTopic, someData)
                except:
                    log.exception("notifyObservers::all: topic: %r, data: %r", aTopic, someData)

    # nsISimpleEnumerator enumerateObservers( in string aTopic );
    def enumerateObservers(self, aTopic):
        self.cv.acquire()
        try:
            vals = self._getLiveObservers(aTopic)
        finally:
            self.cv.release()
        if vals is None:
            vals = []
        return SimpleEnumerator(vals)

