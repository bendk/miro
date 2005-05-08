from urllib import urlopen
from datetime import datetime,timedelta
from threading import RLock
from database import defaultDatabase
from item import *
from scheduler import ScheduleEvent
from copy import copy
from xhtmltools import unescape,xhtmlify
import config

# Universal Feed Parser http://feedparser.org/
# Licensed under Python license
import feedparser

##
# Generates an appropriate feed for a URL
#
# Eventually, we can use this to determine the type of feed automatically
class FeedFactory:
    def __init__(self, config = None):
        self.config = config
	
    ##
    # Returns an appropriate feed for the given URL
    #
    # @param url The URL of the feed
    def gen(self, url):
        if self.isRSSURL(url):
            return RSSFeed(url)
        else:
            return None

    ##
    # Determines the mime type of the URL
    # Returns true IFF url is RSS URL
    def isRSSURL(self,url):
        return True

##
# A feed contains a set of of downloadable items
class Feed(DDBObject):
    def __init__(self, url, title = None, visible = True):
        self.url = url
        self.items = []
	if title == None:
	    self.title = url
	else:
	    self.title = title
        self.created = datetime.now()
        self.lock = RLock()
	self.autoDownloadable = False
	self.getEverything = False
	self.maxNew = -1
	self.fallBehind = -1
	self.expire = "system"
        self.updateFreq = 60*60
	self.startfrom = datetime.min
	self.visible = visible
        DDBObject.__init__(self)

    ##
    # Downloads the next available item taking into account maxNew,
    # fallbehind, and getEverything
    def downloadNext(self, dontUse = []):
	self.lock.acquire()
	try:
	    next = None

	    #The number of items downloading from this feed
	    dling = 0
	    #The number of items eligibile to download
	    eligibile = 0
	    #The number of unwatched, downloaded items
	    newitems = 0

	    #Find the next item we should get
	    for item in self.items:
		if (self.getEverything or item.getPubDateParsed() >= self.startfrom) and item.getState() == "stopped" and not item in dontUse:
		    eligibile += 1
		    if next == None:
			next = item
		    elif item.getPubDateParsed() < next.getPubDateParsed():
			next = item
		if item.getState() == "downloading":
		    dling += 1
		if item.getState() == "finished" or item.getState() == "uploading" and not item.getSeen():
		    newitems += 1

	finally:
	    self.lock.release()

	if self.maxNew >= 0 and newItems >= self.maxNew:
	    return False
	elif self.fallBehind>=0 and eligibile > self.fallBehind:
	    dontUse.append(next)
	    return self.downloadNext(dontUse)
	elif next != None:
	    self.lock.acquire()
	    try:
		self.startfrom = next.getPubDateParsed()
	    finally:
		self.lock.release()
	    next.download(autodl = True)
	    return True
	else:
	    return False

    ##
    # Returns marks expired items as expired
    def expireItems(self):
	if self.expire == "feed":
	    expireTime = self.expireTime
	elif self.expire == "system":
	    expireTime = config.get('DefaultTimeUntilExpiration')
	elif self.expire == "never":
	    return
	for item in self.items:
	    if item.getState() == "finished" and datetime.now() - item.getDownloadedTime() > expireTime:
		item.expire()

    ##
    # Returns true iff feed should be visible
    def isVisible(self):
	self.lock.acquire()
	try:
	    ret = self.visible
	finally:
	    self.lock.release()
	return ret

    ##
    # Takes in parameters from the save settings page and saves them
    def saveSettings(self,automatic,maxnew,fallBehind,expire,expireDays,expireHours,getEverything):
	self.lock.acquire()
	try:
	    self.autoDownloadable = (automatic == "1")
	    self.getEverything = (getEverything == "1")
	    if maxnew == "unlimited":
		self.maxNew = -1
	    else:
		self.maxNew = int(maxnew)
	    if fallBehind == "unlimited":
		self.fallBehind = -1
	    else:
		self.fallBehind = int(fallBehind)
	    self.expire = expire
	    self.expireTime = timedelta(days=int(expireDays),hours=int(expireHours))
	finally:
	    self.lock.release()

    ##
    # Returns "feed," "system," or "never"
    def getExpirationType(self):
	self.lock.acquire()
	ret = self.expire
	self.lock.release()
	return ret

    ##
    # Returns"unlimited" or the maximum number of items this feed can fall behind
    def getMaxFallBehind(self):
	self.lock.acquire()
	if self.fallBehind < 0:
	    ret = "unlimited"
	else:
	    ret = self.fallBehind
	self.lock.release()
	return ret

    ##
    # Returns "unlimited" or the maximum number of items this feed wants
    def getMaxNew(self):
	self.lock.acquire()
	if self.maxNew < 0:
	    ret = "unlimited"
	else:
	    ret = self.maxNew
	self.lock.release()
	return ret

    ##
    # Returns the number of days until a video expires
    def getExpireDays(self):
	ret = 0
	self.lock.acquire()
	try:
	    try:
		ret = self.expireTime.days
	    except:
		ret = config.get('DefaultTimeUntilExpiration').days
	finally:
	    self.lock.release()
	return ret

    ##
    # Returns the number of hours until a video expires
    def getExpireHours(self):
	ret = 0
	self.lock.acquire()
	try:
	    try:
		ret = int(self.expireTime.seconds/3600)
	    except:
		ret = int(config.get('DefaultTimeUntilExpiration').seconds/3600)
	finally:
	    self.lock.release()
	return ret
	

    ##
    # Returns true iff item is autodownloadable
    def isAutoDownloadable(self):
        self.lock.acquire()
        ret = self.autoDownloadable
        self.lock.release()
        return ret


    ##
    # Returns the title of the feed
    def getTitle(self):
        self.lock.acquire()
        ret = self.title
        self.lock.release()
        return ret

    ##
    # Returns the URL of the feed
    def getURL(self):
        self.lock.acquire()
        ret = self.url
        self.lock.release()
        return ret

    ##
    # Returns the description of the feed
    def getDescription(self):
        return ""

    ##
    # Returns a link to a webpage associated with the feed
    def getLink(self):
        return ""

    ##
    # Returns the URL of the library associated with the feed
    def getLibraryLink(self):
        return ""

    ##
    # Returns the URL of a thumbnail associated with the feed
    def getThumbnail(self):
	return ""

    ##
    # Returns URL of license assocaited with the feed
    def getLicense(self):
	return ""

    ##
    # Returns the number of new items with the feed
    def getNewItems(self):
        self.lock.acquire()
	count = 0
	for item in self.items:
	    try:
		if item.getState() == 'finished' and not item.getSeen():
		    count += 1
	    except:
		pass
        self.lock.release()
        return count

    ##
    # Removes a feed from the database
    def removeFeed(self,url):
        self.lock.acquire()
        try:
            for item in self.items:
                item.remove()
        finally:
            self.lock.release()
        self.remove()

    ##
    # Called by pickle during serialization
    def __getstate__(self):
	temp = copy(self.__dict__)
	temp["lock"] = None
	return temp

    ##
    # Called by pickle during deserialization
    def __setstate__(self,state):
	self.__dict__ = state
	self.lock = RLock()

class RSSFeed(Feed):
    def __init__(self,url,title = None):
        Feed.__init__(self,url,title)
        self.scheduler = ScheduleEvent(self.updateFreq, self.update)
	self.itemlist = defaultDatabase.filter(lambda x:isinstance(x,Item) and x.feed is self)
        self.scheduler = ScheduleEvent(0, self.update,False)

    ##
    # Returns the description of the feed
    def getDescription(self):
	self.lock.acquire()
	try:
	    ret = xhtmlify('<span>'+unescape(self.parsed.summary)+'</span>')
	except:
	    ret = ""
	self.lock.release()
        return ret

    ##
    # Returns a link to a webpage associated with the feed
    def getLink(self):
	self.lock.acquire()
	try:
	    ret = self.parsed.link
	except:
	    ret = ""
	self.lock.release()
        return ret

    ##
    # Returns the URL of the library associated with the feed
    def getLibraryLink(self):
        self.lock.acquire()
	try:
	    ret = self.parsed.libraryLink
	except:
	    ret = ""
        self.lock.release()
        return ret

    ##
    # Returns the URL of a thumbnail associated with the feed
    def getThumbnail(self):
        self.lock.acquire()
	try:
	    ret = self.parsed.image.url
	except:
	    ret = ""
        self.lock.release()
        return ret
	

    ##
    # Updates a feed
    def update(self):
	try:
	    d = feedparser.parse(self.url,etag=self.parsed.etag,modified=self.parsed.modified)
	    if d.status == 304:
		return ""
	    else:
		self.parsed = d
	except:
	    self.parsed = feedparser.parse(self.url)
        self.lock.acquire()
        try:
	    try:
		if self.parsed.status == 301: #permanent redirect
                    self.url = self.parsed.url
	    except:
		pass
            try:
                self.title = self.parsed["feed"]["title"]
            except KeyError:
                try:
                    self.title = self.parsed["channel"]["title"]
                except KeyError:
                    pass
            for entry in self.parsed.entries:
                new = True
                for item in self.items:
                    try:
                        if item.getRSSID() == entry["id"]:
                            item.update(entry)
                            new = False
                    except KeyError:
                        # If the item changes at all, it results in a
                        # new entry
                        if (item.getRSSEntry() == entry):
                            item.update(entry)
                            new = False
                if new:
                    self.items.append(Item(self,entry))
            try:
                self.updateFreq = min(15*60,self.parsed["feed"]["ttl"]*60)
            except KeyError:
                self.updateFreq = 60*60
        finally:
            self.lock.release()
	    self.beginChange()
	    self.endChange()
    ##
    # Overrides the DDBObject remove()
    def remove(self):
        self.scheduler.remove()
        Feed.remove(self)

    ##
    # Returns the URL of the license associated with the feed
    def getLicense(self):
	try:
	    ret = self.parsed.license
	except:
	    ret = ""
	return ret

    ##
    # Called by pickle during serialization
    def __getstate__(self):
	temp = copy(self.__dict__)
	temp["lock"] = None
	temp["itemlist"] = None
	temp["scheduler"] = None
	return temp

    ##
    # Called by pickle during deserialization
    def __setstate__(self,state):
	self.__dict__ = state
	self.lock = RLock()
	self.itemlist = defaultDatabase.filter(lambda x:isinstance(x,Item) and x.feed is self)
        self.scheduler = ScheduleEvent(self.updateFreq, self.update)

	#FIXME: the update dies if all of the items aren't restored, so we 
        # wait a little while before we start the update
        self.scheduler = ScheduleEvent(5, self.update,False)

##
# A DTV Collection of items -- similar to a playlist
class Collection(Feed):
    def __init__(self,title = None):
        Feed.__init__(self,url = "dtv:collection",title = title,visible = False)

    ##
    # Adds an item to the collection
    def addItem(self,item):
	if isinstance(item,Item):
	    self.lock.acquire()
	    try:
		self.removeItem(item)
		self.items.append(item)
	    finally:
		self.lock.release()
	    return True
	else:
	    return False

    ##
    # Moves an item to another spot in the collection
    def moveItem(self,item,pos):
	self.lock.acquire()
	try:
	    self.removeItem(item)
	    if pos < len(self.items):
		self.items[pos:pos] = [item]
	    else:
		self.items.append(item)
	finally:
	    self.lock.release()

    ##
    # Removes an item from the collection
    def removeItem(self,item):
	self.lock.acquire()
	try:
	    for x in range(0,len(self.items)):
		if self.items[x] == item:
		    self.items[x:x+1] = []
		    break
	finally:
	    self.lock.release()
	return True
