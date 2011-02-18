# Miro - an RSS based video player application
# Copyright (C) 2010 Participatory Culture Foundation
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

import errno
import os
import socket
import select
import struct
import threading
import time
import uuid

from hashlib import md5

from miro.gtcache import gettext as _
from miro import app
from miro import eventloop
from miro import messages
from miro import playlist
from miro import prefs
from miro import signals
from miro import util
from miro.fileobject import FilenameType
from miro.util import returns_filename

from miro.plat import resources
from miro.plat.utils import thread_body

try:
    import libdaap
except ImportError:
    from miro import libdaap

DAAP_META = ('dmap.itemkind,dmap.itemid,dmap.itemname,' +
             'dmap.containeritemid,dmap.parentcontainerid,' +
             'daap.songtime,daap.songsize,daap.songformat,' +
             'daap.songartist,daap.songalbum,daap.songgenre,' +
             'daap.songyear,daap.songtracknumber,daap.songuserrating,' +
             'com.apple.itunes.mediakind')

# Conversion factor between our local duration (10th of a second)
# vs daap which is millisecond.
DURATION_SCALE = 1000

daap_mapping = {
    'daap.songformat': 'enclosure',
    'com.apple.itunes.mediakind': 'file_type',
    'dmap.itemid': 'id',
    'dmap.itemname': 'name',
    'daap.songtime': 'duration',
    'daap.songsize': 'size',
    'daap.songartist': 'artist',
    'daap.songalbumartist': 'album_artist',
    'daap.songalbum': 'album',
    'daap.songyear': 'year',
    'daap.songgenre': 'genre',
    'daap.songtracknumber': 'track'
}

daap_rmapping = {
    'enclosure': 'daap.songformat',
    'file_type': 'com.apple.itunes.mediakind',
    'id': 'dmap.itemid',
    'name': 'dmap.itemname',
    'duration': 'daap.songtime',
    'size': 'daap.songsize',
    'artist': 'daap.songartist',
    'album_artist': 'daap.songalbumartist',
    'album': 'daap.songalbum',
    'year': 'daap.songyear',
    'genre': 'daap.songgenre',
    'track': 'daap.songtracknumber'
}

# Windows Python does not have inet_ntop().  Sigh.  Fallback to this one,
# which isn't as good, if we do not have access to it.
def inet_ntop(af, ip):
    try:
        return socket.inet_ntop(af, ip)
    except AttributeError:
        if af == socket.AF_INET:
            return socket.inet_ntoa(ip)
        if af == socket.AF_INET6:
            return ':'.join('%x' % bit for bit in struct.unpack('!' + 'H' * 8,
                                                                ip))
        raise ValueError('unknown address family %d' % af)

class SharingItem(object):
    """
    An item which lives on a remote share.
    """
    def __init__(self, **kwargs):
        for required in ('video_path', 'id', 'file_type', 'host', 'port'):
            if required not in kwargs:
                raise TypeError('SharingItem must be given a "%s" argument'
                                % required)
        self.name = self.file_format = self.size = None
        self.release_date = self.feed_name = self.feed_id = None
        self.keep = self.media_type_checked = True
        self.isContainerItem = False
        self.url = self.payment_link = None
        self.comments_link = self.permalink = self.file_url = None
        self.license = self.downloader = None
        self.duration = self.screenshot = self.thumbnail_url = None
        self.resumeTime = 0
        self.subtitle_encoding = self.enclosure_type = None
        self.description = u''
        self.album = None
        self.artist = None
        self.title_tag = None
        self.track = None
        self.year = None
        self.genre = None
        self.metadata_version = 0
        self.rating = None
        self.file_type = None
        self.creation_time = None

        self.__dict__.update(kwargs)

        self.video_path = FilenameType(self.video_path)
        if self.name is None:
            self.name = _("Unknown")
        # Do we care about file_format?
        if self.file_format is None:
            pass
        if self.size is None:
            self.size = 0
        if self.release_date is None or self.creation_time is None:
            now = time.time()
            if self.release_date is None:
                self.release_date = now
            if self.creation_time is None:
                self.creation_time = now
        if self.duration is None: # -1 is unknown
            self.duration = 0

    @staticmethod
    def id_exists():
        return True

    def get_release_date(self):
        return self.release_date

    @returns_filename
    def get_filename(self):
        # For daap, sent it to be the same as http as it is basically
        # http with a different port.
        def daap_handler(path, host, port):
            return 'http://%s:%s%s' % (host, port, path)
        fn = FilenameType(self.video_path)
        fn.set_urlize_handler(daap_handler, [self.host, self.port])
        return fn

    def get_url(self):
        return self.url or u''

    @returns_filename
    def get_thumbnail(self):
        # What about cover art?
        if self.file_type == 'audio':
            return resources.path("images/thumb-default-audio.png")
        else:
            return resources.path("images/thumb-default-video.png")

    def _migrate_thumbnail(self):
        # This should not ever do anything useful.  We don't have a backing
        # database to safe this stuff.
        pass

    def remove(self, save=True):
        # This should never do anything useful, we don't have a backing
        # database. Yet.
        pass

class SharingTracker(object):
    """The sharing tracker is responsible for listening for available music
    shares and the main client connection code.  For each connected share,
    there is a separate SharingItemTrackerImpl() instance which is basically
    a backend for messagehandler.SharingItemTracker().
    """
    type = u'sharing'
    # These need to be the same size.
    CMD_QUIT = 'quit'
    CMD_PAUSE = 'paus'
    CMD_RESUME = 'resm'

    def __init__(self):
        self.trackers = dict()
        self.available_shares = dict()
        self.r, self.w = util.make_dummy_socket_pair()
        self.paused = True
        self.event = threading.Event()

    def calc_local_addresses(self):
        # Get our own hostname so that we can filter out ourselves if we 
        # also happen to be broadcasting.  Getaddrinfo() may block so you 
        # MUST call in auxiliary thread context.
        #
        # Why isn't this cached, you may ask?  Because the system may
        # change IP addresses while this program is running then we'd be
        # filtering the wrong addresses.  
        #
        # XXX can I count on the Bonjour daemon implementation to send me
        # the add/remove messages when the IP changes?
        hostname = socket.gethostname()
        local_addresses = []
        try:
            addrinfo = socket.getaddrinfo(hostname, 0, 0, 0, socket.SOL_TCP)
            for family, socktype, proto, canonname, sockaddr in addrinfo:
                local_addresses.append(canonname)
        except socket.error, (err, errstring):
            # What am I supposed to do here?
            pass

        return local_addresses

    def mdns_callback(self, added, fullname, host, port):
        eventloop.add_urgent_call(self.mdns_callback_backend, "mdns callback",
                                  args=[added, fullname, host, port])

    def try_to_add(self, share_id, fullname, host, port, uuid):
        def success(unused):
            info = self.available_shares[share_id]
            # It's been deleted or worse, deleted and recreated!
            if not info or info.connect_uuid != uuid:
                return
            info.connect_uuid = None
            messages.TabsChanged('sharing', [info], [], []).send_to_frontend()

        def failure(unused):
            info = self.available_shares[share_id]
            if not info or info.connect_uuid != uuid:
                return
            info.connect_uuid = None

        def testconnect():
            client = libdaap.make_daap_client(host, port)
            if not client.connect() or client.databases() is None:
                raise IOError('test connect failed')
            client.disconnect()

        eventloop.call_in_thread(success,
                                 failure,
                                 testconnect,
                                 'DAAP test connect')

    def mdns_callback_backend(self, added, fullname, host, port):
        if fullname == app.sharing_manager.name:
            return
        # Need to come up with a unique ID for the share.  Use the name
        # only since that's supposed to be unique.  We rely on the 
        # zeroconf daemon not telling us garbage.  Why name only though?
        # Because on removal, Avahi can't do a name query, so we have no
        # hostname, port, or IP address information!
        share_id = unicode(fullname)
        print 'gotten MDNS CALLBACK share_id'
        # Do we have this share on record?  If so then just ignore.
        # In particular work around a problem with Avahi apparently sending
        # duplicate messages, maybe it's doing that once for IPv4 then again
        # for IPv6?
        if added and share_id in self.available_shares.keys():
            return
        if not added and not share_id in self.available_shares.keys():
            return 

        if added:
            # Create the SharingInfo eagerly, so that duplicate messages
            # can use it to filter out.  We also create a unique stamp on it,
            # in case of errant implementations that try to register, delete,
            # and re-register the share.  The try_to_add() success/failure
            # callback can check whether info is still valid and if so, if it
            # is this particular info (if not, the uuid will be different and
            # and so should ignore).
            info = messages.SharingInfo(share_id, fullname, host, port)
            info.connect_uuid = uuid.uuid4()
            self.available_shares[share_id] = info
            self.try_to_add(share_id, fullname, host, port, info.connect_uuid)
        else:
            # The mDNS publish is going away.  Are we connected?  If we
            # are connected, keep it around.  If not, make it disappear.
            # SharingDisappeared() kicks off the necessary bits in the 
            # frontend for us.
            # Future work: we may want to update the name of the share in the
            # sidebar if we detect it is actually a rename?
            if not share_id in self.trackers.keys():
                victim = self.available_shares[share_id]
                del self.available_shares[share_id]
                # Only tell the frontend if the share's been tested because
                # otherwise the TabsChanged() message wouldn't have arrived.
                if victim.connect_uuid is None:
                    messages.SharingDisappeared(victim).send_to_frontend()

    def server_thread(self):
        # Wait for the resume message from the sharing manager as 
        # startup protocol of this thread.
        while True:
            try:
                r, w, x = select.select([self.r], [], [])
                if self.r in r:
                    cmd = self.r.recv(4)
                    if cmd == SharingTracker.CMD_RESUME:
                        self.paused = False
                        break
                    # User quit very quickly.
                    elif cmd == SharingTracker.CMD_QUIT:
                        return
                    raise ValueError('bad startup message received')
            except select.error, (err, errstring):
                if err == errno.EINTR:
                    continue
            except StandardError, err:
                raise ValueError('unknown error during select %s' % str(err))

        if app.sharing_manager.mdns_present:
            callback = libdaap.mdns_browse(self.mdns_callback)
        else:
            callback = None
        while True:
            refs = []
            if callback is not None and not self.paused:
                refs = callback.get_refs()
            try:
                # Once we get a shutdown signal (from self.r/self.w socketpair)
                # we return immediately.  I think this is okay since we are 
                # passive listener and we only stop tracking on shutdown,
                #  OS will help us close all outstanding sockets including that
                # for this listener when this process terminates.
                r, w, x = select.select(refs + [self.r], [], [])
                if self.r in r:
                    cmd = self.r.recv(4)
                    if cmd == SharingTracker.CMD_QUIT:
                        return
                    if cmd == SharingTracker.CMD_PAUSE:
                        self.paused = True
                        self.event.set()
                        continue
                    if cmd == SharingTracker.CMD_RESUME:
                        self.paused = False
                        continue
                    raise
                for i in r:
                    if i in refs:
                        callback(i)
            # XXX what to do in case of error?  How to pass back to user?
            except select.error, (err, errstring):
                if err == errno.EINTR:
                    continue
                else:
                    pass
            except StandardError:
                pass

    def start_tracking(self):
        # sigh.  New thread.  Unfortunately it's kind of hard to integrate
        # it into the application runloop at this moment ...
        self.thread = threading.Thread(target=thread_body,
                                       args=[self.server_thread],
                                       name='mDNS Browser Thread')
        self.thread.start()

    def eject(self, share_id):
        tracker = self.trackers[share_id]
        del self.trackers[share_id]
        tracker.client_disconnect()

    def get_tracker(self, share_id):
        try:
            return self.trackers[share_id]
        except KeyError:
            print 'CREATING NEW TRACKER'
            share = self.available_shares[share_id]
            self.trackers[share_id] = SharingItemTrackerImpl(share)
            return self.trackers[share_id]

    def stop_tracking(self):
        # What to do in case of socket error here?
        self.w.send(SharingTracker.CMD_QUIT)

    # pause/resume is only meant to be used by the sharing manager.
    # Pause needs to be synchronous because we want to make sure this module
    # is in a quiescent state.
    def pause(self):
        # What to do in case of socket error here?
        self.w.send(SharingTracker.CMD_PAUSE)
        self.event.wait()
        self.event.clear()

    def resume(self):
        # What to do in case of socket error here?
        self.w.send(SharingTracker.CMD_RESUME)

# Synchronization issues: this code is a bit sneaky, so here is an explanation
# of how it works.  When you click on a share tab in the frontend, the 
# display (the item list controller) starts tracking the items.  It does
# so by sending a message to the backend.  If it was previously unconnected
# a new SharingItemTrackerImpl() will be created, and connect() is called,
# which may take an indeterminate period of time, so this is farmed off
# to an external thread.  When the connection is successful, a callback will
# be called which is run on the backend (eventloop) thread which adds the
# items and playlists to the SharingItemTrackerImpl tracker object. 
# At the same time, handle_item_list() is called after the tracker is created
# which will be empty at this time, because the items have not yet been added.
# (recall that the callback runs in the eventloop, we are already in the 
# eventloop so this could not have happened prior to handle_item_list()
# being called).
#
# The SharingItemTrackerImpl() object is designed to be persistent until
# disconnection happens.  If you click on a tab that's already connected,
# it finds the appropriate tracker and calls handle_item_list.  Either it is
# already populated, or if connection is still in process will return empty
# list until the connection success callback is called.
class SharingItemTrackerImpl(signals.SignalEmitter):
    """This is the backend for the SharingItemTracker the messagehandler file.
    This backend class allows the item tracker to be persistent even as the
    user switches across different tabs in the sidebar, until the disconnect
    button is clicked.
    """
    type = u'sharing'
    def __init__(self, share):
        self.client = None
        self.share = share
        self.items = []
        self.playlists = []
        self.base_playlist = None    # Temporary
        eventloop.call_in_thread(self.client_connect_callback,
                                 self.client_connect_error_callback,
                                 self.client_connect,
                                 'DAAP client connect')
        signals.SignalEmitter.__init__(self)
        for sig in 'added', 'changed', 'removed':
            self.create_signal(sig)

    def sharing_item(self, rawitem, playlist_id):
        kwargs = dict()
        for k in rawitem.keys():
            try:
                key = daap_mapping[k]
            except KeyError:
                # Got something back we don't really care about.
                continue
            kwargs[key] = rawitem[k]
            if isinstance(rawitem[k], str):
                kwargs[key] = kwargs[key].decode('utf-8')

        # Fix this up.
        file_type = u'audio'    # fallback
        if kwargs['file_type'] == libdaap.DAAP_MEDIAKIND_AUDIO:
            file_type = u'audio'
        if kwargs['file_type'] in [libdaap.DAAP_MEDIAKIND_TV,
                                    libdaap.DAAP_MEDIAKIND_MOVIE,
                                    libdaap.DAAP_MEDIAKIND_VIDEO
                                   ]:
            file_type = u'video'
        kwargs['file_type'] = file_type
        kwargs['video_path'] = self.client.daap_get_file_request(
                                   kwargs['id'],
                                   kwargs['enclosure'])
        kwargs['host'] = self.client.host
        kwargs['port'] = self.client.port
        kwargs['file_type'] = file_type
        kwargs['playlist_id'] = playlist_id

        # Duration: daap uses millisecond, so we need to scale it.
        if kwargs['duration'] is not None:
            kwargs['duration'] /= DURATION_SCALE

        sharing_item = SharingItem(**kwargs)
        return sharing_item

    def client_disconnect(self):
        client = self.client
        self.client = None
        playlist_ids = [playlist_.id for playlist_ in self.playlists]
        message = messages.TabsChanged(self.type, [], [], playlist_ids)
        message.send_to_frontend()
        eventloop.call_in_thread(self.client_disconnect_callback,
                                 self.client_disconnect_error_callback,
                                 client.disconnect,
                                 'DAAP client connect')

    def client_disconnect_error_callback(self, unused):
        pass

    def client_disconnect_callback(self, unused):
        pass

    def client_connect(self):
        name = self.share.name
        host = self.share.host
        port = self.share.port
        returned_items = []
        returned_playlists = []
        self.client = libdaap.make_daap_client(host, port)
        if not self.client.connect():
            # XXX API does not allow us to send more detailed results
            # back to the poor user.
            raise IOError('Cannot connect')
        if not self.client.databases():
            raise IOError('Cannot get database')
        playlists = self.client.playlists()
        if playlists is None:
            raise IOError('Cannot get playlist')
        for k in playlists.keys():
            is_base_playlist = None
            if playlists[k].has_key('daap.baseplaylist'):
                is_base_playlist = playlists[k]['daap.baseplaylist']
            if is_base_playlist:
                if self.base_playlist:
                    print 'WARNING: more than one base playlist found'
                self.base_playlist = k
            # This isn't the playlist id of the remote share, this is the
            # playlist id we use internally.
            # XXX is there anything better we can do than repr()?
            if not is_base_playlist:
                # XXX only add playlist if it not base playlist.  We don't
                # explicitly show base playlist.
                playlist_id = unicode(md5(repr((name,
                                                host,
                                                port, k))).hexdigest())
                info = messages.SharingInfo(playlist_id,
                                            playlists[k]['dmap.itemname'],
                                            host,
                                            port,
                                            parent_id=self.share.id,
                                            playlist_id=k)
                returned_playlists.append(info)

        # Maybe we have looped through here without a base playlist.  Then
        # the server is broken?
        if not self.base_playlist:
            raise ValueError('Cannot find base playlist')

        items = self.client.items(playlist_id=self.base_playlist,
                                  meta=DAAP_META)
        # XXX FIXME: organize this much better with a dict from ground up
        itemdict = dict()    # XXX temporary band-aid
        for itemkey in items.keys():
            item = self.sharing_item(items[itemkey], self.base_playlist)
            itemdict[itemkey] = items[itemkey]
            returned_items.append(item)

        # Have to save the items from the base playlist first, because
        # Rhythmbox will get lazy and only send the ids around (expecting
        # us to already to have the data, I guess). 
        for k in playlists.keys():
            if k == self.base_playlist:
                continue
            items = self.client.items(playlist_id=k, meta=DAAP_META)
            for itemkey in items.keys():
                rawitem = itemdict[itemkey]
                item = self.sharing_item(rawitem, k)
                returned_items.append(item)

        # We don't append these items directly to the object and let
        # the success callback to do it to prevent race.
        return (returned_items, returned_playlists)

    # NB: this runs in the eventloop (backend) thread.
    def client_connect_callback(self, args):
        returned_items, returned_playlists = args
        self.items = returned_items
        self.playlists = returned_playlists
        self.share.mount = True
        message = messages.TabsChanged('sharing', self.playlists,
                                       [self.share], [])
        message.send_to_frontend()
        # Send a list of all the items to the main sharing tab.  Only add
        # those that are part of o the base playlist.
        for item in self.items:
            if item.playlist_id == self.base_playlist:
                self.emit('added', item)

    def client_connect_error_callback(self, unused):
        # If it didn't work, immediately disconnect ourselves.
        app.sharing_tracker.eject(self.share.id)
        messages.SharingConnectFailed(self.share).send_to_frontend()

    def get_items(self, playlist_id=None):
        # XXX SLOW!  And could possibly do with some refactoring.
        if not playlist_id and self.base_playlist is not None:
            return [item for item in self.items if
                    item.playlist_id == self.base_playlist]
        else:
            return [item for item in self.items if  
                    item.playlist_id == playlist_id]
      

class SharingManagerBackend(object):
    """SharingManagerBackend is the bridge between pydaap and Miro.  It
    pushes Miro media items to pydaap so pydaap can serve them to the outside
    world."""
    type = u'sharing-backend'
    id = u'sharing-backend'
    daapitems = dict()          # DAAP format XXX - index via the items
    # XXX daapplaylist should be hidden from view. 
    daap_playlists = dict()     # Playlist, in daap format
    playlist_item_map = dict()  # Playlist -> item mapping

    # Reserved for future use: you can register new sharing protocols here.
    def register_protos(self, proto):
        pass

    def handle_item_list(self, message):
        self.make_item_dict(message.items)

    def handle_items_changed(self, message):
        # If items are changed, just redelete and recreate the entry.
        for itemid in message.removed:
            del self.daapitems[itemid]
        self.make_item_dict(message.added)
        self.make_item_dict(message.changed)

    def make_daap_playlists(self, items):
        for item in items:
            itemprop = dict()
            for attr in daap_rmapping.keys():
               daap_string = daap_rmapping[attr]
               itemprop[daap_string] = getattr(item, attr, None)
               # XXX Pants.
               if (daap_string == 'dmap.itemname' and
                 itemprop[daap_string] == None):
                   itemprop[daap_string] = getattr(item, 'title', None)
               if isinstance(itemprop[daap_string], unicode):
                   itemprop[daap_string] = (
                     itemprop[daap_string].encode('utf-8'))
            daap_string = 'dmap.itemcount'
            if daap_string == 'dmap.itemcount':
                # At this point, the item list has not been fully populated 
                # yet.  Therefore, it may not be possible to run 
                # get_items() and getting the count attribute.  Instead we 
                # use the playlist_item_map.
                tmp = [y for y in 
                       playlist.PlaylistItemMap.playlist_view(item.id)]
                count = len(tmp)
                itemprop[daap_string] = count
            daap_string = 'dmap.parentcontainerid'
            if daap_string == 'dmap.parentcontainerid':
                itemprop[daap_string] = 0
                #attributes.append(('mpco', 0)) # Parent container ID
                #attributes.append(('mimc', count))    # Item count
                #self.daap_playlists[x.id] = attributes
            daap_string = 'dmap.persistentid'
            if daap_string == 'dmap.persistentid':
                itemprop[daap_string] = item.id
            self.daap_playlists[item.id] = itemprop

    def handle_playlist_added(self, obj, added):
        playlists = [x for x in added if not x.is_folder]
        eventloop.add_urgent_call(lambda: self.make_daap_playlists(playlists),
                                  "SharingManagerBackend: playlist added")

    def handle_playlist_changed(self, obj, changed):
        def _handle_playlist_changed():
            # We could just overwrite everything without actually deleting
            # the object.  A missing key means it's a folder, and we skip
            # over it.
            for x in changed:
                if self.daap_playlists.has_key(x.id):
                    del self.daap_playlists[x.id]
            self.make_daap_playlists(changed)
        eventloop.add_urgent_call(lambda: _handle_playlist_changed(),
                                  "SharingManagerBackend: playlist changed")

    def handle_playlist_removed(self, obj, removed):
        def _handle_playlist_removed():
            for x in removed:
                # Missing key means it's a folder and we skip over it.
                if self.daap_playlists.has_key(x):
                    del self.daap_playlists[x]
        eventloop.add_urgent_call(lambda: _handle_playlist_removed(),
                                  "SharingManagerBackend: playlist removed")

    def populate_playlists(self):
        self.make_daap_playlists(playlist.SavedPlaylist.make_view())
        for playlist_id in self.daap_playlists.keys():
            self.playlist_item_map[playlist_id] = [x.item_id
              for x in playlist.PlaylistItemMap.playlist_view(playlist_id)]

    def start_tracking(self):
        app.info_updater.item_list_callbacks.add(self.type, self.id,
                                                 self.handle_item_list)
        app.info_updater.item_changed_callbacks.add(self.type, self.id,
                                                    self.handle_items_changed)
        messages.TrackItems(self.type, self.id).send_to_backend()

        self.populate_playlists()

        app.info_updater.connect('playlists-added',
                                 self.handle_playlist_added)
        app.info_updater.connect('playlists-changed',
                                 self.handle_playlist_changed)
        app.info_updater.connect('playlists-removed',
                                 self.handle_playlist_removed)

    def stop_tracking(self):
        messages.StopTrackingItems(self.type, self.id).send_to_backend()
        app.info_updater.item_list_callbacks.remove(self.type, self.id,
                                                    self.handle_item_list)
        app.info_updater.item_changed_callbacks.remove(self.type, self.id,
                                                    self.handle_items_changed)

        app.info_updater.disconnect(self.handle_playlist_added)
        app.info_updater.disconnect(self.handle_playlist_changed)
        app.info_updater.disconnect(self.handle_playlist_removed)

    def get_filepath(self, itemid):
        return self.daapitems[itemid]['path']

    def get_playlists(self):
        return self.daap_playlists

    def get_items(self, playlist_id=None):
        # Easy: just return
        if not playlist_id:
            return self.daapitems
        # XXX Somehow cache this?
        print 'GET_ITEMS', playlist_id
        playlist = dict()
        for x in self.daapitems.keys():
            if x in self.playlist_item_map[playlist_id]:
                playlist[x] = self.daapitems[x]
        return playlist

    def make_item_dict(self, items):
        # See the daap_rmapping/daap_mapping for a list of mappings that
        # we do.
        for item in items:
            itemprop = dict()
            for attr in daap_rmapping.keys():
                daap_string = daap_rmapping[attr]
                itemprop[daap_string] = getattr(item, attr, None)
                if isinstance(itemprop[daap_string], unicode):
                    itemprop[daap_string] = (
                      itemprop[daap_string].encode('utf-8'))
                # Fixup the year, etc being -1.  XXX should read the daap
                # type then determine what to do.
                if itemprop[daap_string] == -1:
                    itemprop[daap_string] = 0
                # Fixup: these are stored as string?
                if daap_string in ('daap.songtracknumber',
                                   'daap.songyear'):
                    if itemprop[daap_string] is not None:
                        itemprop[daap_string] = int(itemprop[daap_string])
                # Fixup the duration: need to convert to millisecond.
                if daap_string == 'daap.songtime':
                    itemprop[daap_string] *= DURATION_SCALE
            # Fixup the enclosure format.
            f, e = os.path.splitext(item.video_path)
            # Note! sometimes this doesn't work because the file has no
            # extension!
            e = e[1:] if e else None
            if isinstance(e, unicode):
                e = e.encode('utf-8')
            itemprop['daap.songformat'] = e
            # Fixup the media kind: XXX what about u'other'?
            if itemprop['com.apple.itunes.mediakind'] == u'video':
                itemprop['com.apple.itunes.mediakind'] = (
                  libdaap.DAAP_MEDIAKIND_VIDEO)
            else:
                itemprop['com.apple.itunes.mediakind'] = (
                  libdaap.DAAP_MEDIAKIND_AUDIO)
            # don't forget to set the path..
            # ok: it is ignored since this is not valid dmap/daap const.
            itemprop['path'] = item.video_path
            self.daapitems[item.id] = itemprop

class SharingManager(object):
    """SharingManager is the sharing server.  It publishes Miro media items
    to the outside world.  One part is the server instance and the other
    part is the service publishing, both are handled here.

    Important note: mdns_present only indicates the ability to interact with
    the mdns libraries, does not mean that mdns functionality is present
    on the system (e.g. server may be disabled).
    """
    # These commands should all be of the same size.
    CMD_QUIT = 'quit'
    CMD_NOP  = 'noop'
    def __init__(self):
        self.r, self.w = util.make_dummy_socket_pair()
        self.sharing = False
        self.discoverable = False
        self.name = ''
        self.mdns_present = libdaap.mdns_init()
        self.mdns_callback = None
        self.callback_handle = app.backend_config_watcher.connect('changed',
                               self.on_config_changed)
        # Create the sharing server backend that keeps track of all the list
        # of items available.  Don't know whether we can just query it on the
        # fly, maybe that's a better idea.
        self.backend = SharingManagerBackend()
        # We can turn it on dynamically but if it's not too much work we'd
        # like to get these before so that turning it on and off is not too
        # onerous?
        self.backend.start_tracking()
        # Enable sharing if necessary.
        self.twiddle_sharing()
        if not self.mdns_present:
            app.sharing_tracker.resume()

    def session_count(self):
        if self.sharing:
            return self.server.session_count()
        else:
            return 0

    def on_config_changed(self, obj, key, value):
        listen_keys = [prefs.SHARE_MEDIA.key,
                       prefs.SHARE_DISCOVERABLE.key,
                       prefs.SHARE_NAME.key]
        if not key in listen_keys:
            return
        self.twiddle_sharing()

    def twiddle_sharing(self):
        sharing = app.config.get(prefs.SHARE_MEDIA)
        discoverable = app.config.get(prefs.SHARE_DISCOVERABLE)
        name = app.config.get(prefs.SHARE_NAME).encode('utf-8')
        name_changed = name != self.name
        if sharing != self.sharing:
            if sharing:
                # TODO: if this didn't work, should we set a timer to retry
                # at some point in the future?
                if not self.enable_sharing():
                    # if it didn't work then it must be false regardless.
                    self.discoverable = False
                    return
            else:
                if self.discoverable:
                    self.disable_discover()
                self.disable_sharing()

        # Short-circuit: if we have just disabled the share, then we don't
        # need to check the discoverable bits since it is not relevant, and
        # would already have been disabled anyway.
        if not self.sharing:
            return

        # Did we change the name?  If we have, then disable the share publish
        # first, and update what's kept in the server.
        if name_changed and self.discoverable:
            self.disable_discover()
            app.sharing_tracker.pause()
            self.server.set_name(name)

        if discoverable != self.discoverable:
            if discoverable:
                self.enable_discover()
            else:
                self.disable_discover()

    def get_address(self):
        server_address = (None, None)
        try:
            server_address = self.server.server_address
        except AttributeError:
            pass
        return server_address

    def mdns_register_callback(self, name):
        self.name = name
        app.sharing_tracker.resume()

    def enable_discover(self):
        name = app.config.get(prefs.SHARE_NAME).encode('utf-8')
        # At this point the server must be available, because we'd otherwise
        # have no clue what port to register for with Bonjour.
        address, port = self.server.server_address
        self.mdns_callback = libdaap.mdns_register_service(name,
                                                  self.mdns_register_callback,
                                                  port=port)
        # not exactly but close enough: it's not actually until the
        # processing function gets called.
        self.discoverable = True
        # Reload the server thread: if we are only toggling between it
        # being advertised, then the server loop is already running in
        # the select() loop and won't know that we need to process the
        # registration.
        self.w.send(SharingManager.CMD_NOP)

    def disable_discover(self):
        self.discoverable = False
        if self.mdns_callback:
            old_callback = self.mdns_callback
            self.mdns_callback = None
            libdaap.mdns_unregister_service(old_callback)

    def server_thread(self):
        server_fileno = self.server.fileno()
        while True:
            try:
                rset = [server_fileno, self.r]
                refs = []
                if self.discoverable and self.mdns_callback:
                    refs += self.mdns_callback.get_refs()
                rset += refs
                r, w, x = select.select(rset, [], [])
                for i in r:
                    if i in refs:
                        # Possible that mdns_callback is not valid at this
                        # point, because the this wakeup was a result of
                        # closing of the socket (e.g. during name change
                        # when we unpublish and republish our name).
                        if self.mdns_callback:
                            self.mdns_callback(i)
                        continue
                    if server_fileno == i:
                        self.server.handle_request()
                        continue
                    if self.r == i:
                        cmd = self.r.recv(4)
                        print 'CMD', cmd
                        if cmd == SharingManager.CMD_QUIT:
                            return
                        elif cmd == SharingManager.CMD_NOP:
                            print 'RELOAD'
                            continue
                        else:
                            raise 
            except select.error, (err, errstring):
                if err == errno.EINTR:
                    continue 
                else:
                    pass
            # XXX How to pass error, send message to the backend/frontend?
            except StandardError:
                pass

    def enable_sharing(self):
        # Can we actually enable sharing.  The Bonjour client-side libraries
        # might not be installed.  This could happen if the user previously
        # have the libraries installed and has it enabled, but then uninstalled
        # it in the meantime, so handle this case as fail-safe.
        if not self.mdns_present:
            self.sharing = False
            return

        name = app.config.get(prefs.SHARE_NAME).encode('utf-8')
        self.server = libdaap.make_daap_server(self.backend, debug=True,
                                               name=name)
        if not self.server:
            self.sharing = False
            return
        self.thread = threading.Thread(target=thread_body,
                                       args=[self.server_thread],
                                       name='DAAP Server Thread')
        self.thread.daemon = True
        self.thread.start()
        self.sharing = True

        return self.sharing

    def disable_sharing(self):
        self.sharing = False
        # What to do in case of socket error here?
        self.w.send(SharingManager.CMD_QUIT)
        del self.thread
        del self.server

    def shutdown(self):
        if self.sharing:
            if self.discoverable:
                self.disable_discover()
            self.disable_sharing()
