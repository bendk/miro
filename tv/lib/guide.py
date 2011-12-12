# Miro - an RSS based video player application
# Copyright (C) 2005, 2006, 2007, 2008, 2009, 2010, 2011
# Participatory Culture Foundation
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301 USA
#
# In addition, as a special exception, the copyright holders give
# permission to link the code of portions of this program with the OpenSSL
# library.
#
# You must obey the GNU General Public License in all respects for all of
# the code used other than OpenSSL. If you modify file(s) with this
# exception, you may extend this exception to your version of the file(s),
# but you are not obligated to do so. If you do not wish to do so, delete
# this exception statement from your version. If you delete this exception
# statement from all source files in the program, then also delete it here.

"""``miro.guide`` -- Holds ``ChannelGuide`` class and related things.

``ChannelGuide`` is the class used for storing guides and sites.
"""

import logging
from HTMLParser import HTMLParser, HTMLParseError
from urlparse import urlparse, urljoin

from miro.plat import resources
from miro.database import DDBObject, ObjectNotFoundError
from miro.util import returns_unicode, check_u
from miro import app
from miro import prefs
from miro import httpclient
from miro import iconcache
from miro import fileutil

class ChannelGuide(DDBObject, iconcache.IconCacheOwnerMixin):
    ICON_CACHE_VITAL = True

    # constants for the 'store' variable
    STORE_NOT_STORE, STORE_VISIBLE, STORE_INVISIBLE = range(3)

    def setup_new(self, url, allowedURLs=None):
        check_u(url)
        # FIXME - clean up the allowedURLs thing here
        self.allowedURLs = []
        self.url = url
        self.updated_url = url
        self.title = None
        self.userTitle = None
        self.client = None
        self.lastVisitedURL = None
        self.setup_new_icon_cache()
        self.favicon = None
        self.firstTime = True
        self.store = self.STORE_NOT_STORE
        if url:
            self.historyLocation = 0
            self.history = [self.url]
        else:
            self.historyLocation = None
            self.history = []

        self.setup_common()
        self.download_guide()

    def setup_restored(self):
        self.lastVisitedURL = None
        self.historyLocation = None
        self.history = []
        self.client = None
        if self.store is None:
            self.store = self.STORE_NOT_STORE
        self.setup_common()

    def setup_common(self):
        self.type = u'guide'

    @classmethod
    def site_view(cls):
        default_url = app.config.get(prefs.CHANNEL_GUIDE_URL)
        return cls.make_view('url != ? AND store = ?',
                            (default_url, cls.STORE_NOT_STORE,),
                             check_func=lambda guide: guide.is_site)

    @classmethod
    def guide_view(cls):
        default_url = app.config.get(prefs.CHANNEL_GUIDE_URL)
        return cls.make_view('url = ?', (default_url,))

    @classmethod
    def store_view(cls):
        return cls.make_view('store != ?', (cls.STORE_NOT_STORE,))

    @classmethod
    def visible_store_view(cls):
        def check_func(s):
            return s.store == cls.STORE_VISIBLE
        return cls.make_view('store = ?', (cls.STORE_VISIBLE,),
                             check_func=check_func)

    @classmethod
    def invisible_store_view(cls):
        def check_func(s):
            return s.store == cls.STORE_INVISIBLE
        return cls.make_view('store = ?', (cls.STORE_INVISIBLE,),
                             check_func=check_func)

    @classmethod
    def visible_view(cls):
        return cls.make_view('store != ?', (cls.STORE_INVISIBLE,))

    @classmethod
    def get_by_url(cls, url):
        return cls.make_view('url=?', (url,)).get_singleton()

    def __str__(self):
        return "Miro Guide <%s>" % self.url

    def remove(self):
        if self.client is not None:
            self.client.cancel()
            self.client = None
        self.remove_icon_cache()
        DDBObject.remove(self)

    def get_url(self):
        return self.url

    def is_default(self):
        return self.url == app.config.get(prefs.CHANNEL_GUIDE_URL)

    def is_visible(self):
        return self.store != self.STORE_INVISIBLE

    def is_site(self):
        return (not self.is_default() and self.store == cls.STORE_NOT_STORE)

    def get_folder(self):
        return None

    @returns_unicode
    def get_title(self):
        if self.userTitle:
            return self.userTitle
        elif self.title:
            return self.title
        else:
            return self.get_url()

    def set_title(self, title):
        self.confirm_db_thread()
        self.userTitle = title
        self.signal_change(needs_save=True)

    def guide_downloaded(self, info):
        if not self.id_exists():
            return
        self.client = None
        self.updated_url = unicode(info["updated-url"])
        parser = None
        try:
            parser = GuideHTMLParser(self.updated_url)
            parser.feed(info["body"])
            parser.close()
        except (HTMLParseError, UnicodeDecodeError), parser_error:
            logging.debug("Ignoring error when parsing guide %s: %s",
                          self.updated_url, parser_error)

        if parser:
            if parser.title:
                self.title = unicode(parser.title.strip())
            if parser.favicon and unicode(parser.favicon) != self.favicon:
                self.favicon = unicode(parser.favicon)
                self.icon_cache.request_update(True)

        if self.favicon is None:
            parsed = urlparse(self.updated_url)
            self.favicon = parsed[0] + u"://" + parsed[1] + u"/favicon.ico"
            if self.icon_cache is None:
                # bug 12024.  for some reason, guides can have
                # self.icon_cache = None at this point.  if so, we set
                # up another one.
                self.setup_new_icon_cache()
            self.icon_cache.request_update(True)

        self.extend_history(self.updated_url)
        self.signal_change()

    def guide_error(self, error):
        if not self.id_exists():
            return
        # FIXME - this should display some kind of error page to the user
        logging.warn("Error downloading guide: %s", error)
        self.client = None

    def download_guide(self):
        self.client = httpclient.grab_url(self.get_url(), self.guide_downloaded, self.guide_error)

    def get_favicon_path(self):
        """Returns the path to the favicon file.  It's either the favicon of
        the site or the default icon image.
        """
        if self.icon_cache and self.icon_cache.get_filename():
            return fileutil.expand_filename(self.icon_cache.get_filename())
        return None

    def icon_changed(self):
        self.confirm_db_thread()
        self.signal_change(needs_save=False)

    def get_thumbnail_url(self):
        return self.favicon

    def extend_history(self, url):
        if self.historyLocation is None:
            self.historyLocation = 0
            self.history = [url]
        else:
            if self.history[self.historyLocation] == url: # moved backwards
                return
            if self.historyLocation != len(self.history) - 1:
                self.history = self.history[:self.historyLocation+1]
            self.history.append(url)
            self.historyLocation += 1

    def get_history_url(self, direction):
        # FIXME - this looks unused
        if direction is not None:
            location = self.historyLocation + direction
            if location < 0:
                return
            elif location >= len(self.history):
                return
        else:
            location = 0 # go home
        self.historyLocation = location
        return self.history[self.historyLocation]

class GuideHTMLParser(HTMLParser):
    """Grabs the feed link from the given webpage
    """
    def __init__(self, url):
        self.title = None
        self.in_title = False
        self.baseurl = url
        self.favicon = None
        HTMLParser.__init__(self)

    def handle_starttag(self, tag, attrs):
        attrdict = {}
        for key, value in attrs:
            attrdict[key] = value
        if tag == 'title' and self.title == None:
            self.in_title = True
            self.title = u""
        if (tag == 'link' and attrdict.has_key('rel')
                and attrdict.has_key('type')
                and attrdict.has_key('href')
                and 'icon' in attrdict['rel'].split(' ')
                and attrdict['type'].startswith("image/")):
            self.favicon = urljoin(self.baseurl, attrdict['href']).decode('ascii', 'ignore')

    def handle_data(self, data):
        if self.in_title:
            try:
                self.title += data
            except UnicodeDecodeError:
                # FIXME - not all sites are in utf-8 and we should
                # handle this better
                pass

    def handle_endtag(self, tag):
        if tag == 'title' and self.in_title:
            self.in_title = False

def get_guide_by_url(url):
    try:
        return ChannelGuide.get_by_url(url)
    except ObjectNotFoundError:
        return None

def download_guides():
    for guide in ChannelGuide.visible_view():
        guide.download_guide()
