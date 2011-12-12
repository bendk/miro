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

import datetime
import os
import random
import logging
import time

from miro.gtcache import gettext as _
from miro.database import DDBObject, ObjectNotFoundError
from miro.dl_daemon import daemon, command
from miro.download_utils import (next_free_filename, get_file_url_path,
        next_free_directory, filter_directory_name)
from miro.util import (get_torrent_info_hash, returns_unicode, check_u,
                       returns_filename, unicodify, check_f, to_uni, is_magnet_uri)
from miro import app
from miro import dialogs
from miro import displaytext
from miro import eventloop
from miro import httpclient
from miro import models
from miro import prefs
from miro.plat.utils import samefile, unicode_to_filename
from miro import flashscraper
from miro import fileutil
from miro.fileobject import FilenameType

class DownloadStateManager(object):
    """DownloadStateManager: class to store state information about the
    downloader.

    Commands to the downloader is batched and sent every second.  This is
    based on the premise that commands for a particular download id can 
    be completely superceded by a subsequent command, with the exception
    of a pause/resume pair.  For example, a stop command will completely
    supecede a pause command, so if the 2 are sent in quick succession
    only the stop command will be sent by the downloader.  The exception
    to this rule is the pause/resume pair which acts like matter and
    anti-matter, which will nuke itself when they come into contact
    (but dies with not even a whimper instead of a gorgeous display).
    """
    STOP    = command.DownloaderBatchCommand.STOP
    RESUME  = command.DownloaderBatchCommand.RESUME
    PAUSE   = command.DownloaderBatchCommand.PAUSE
    RESTORE = command.DownloaderBatchCommand.RESTORE

    UPDATE_INTERVAL = 1

    def __init__(self):
        self.total_up_rate = 0
        self.total_down_rate = 0
        # a hash of download ids that the server knows about.
        self.downloads = {}
        self.daemon_starter = None
        self.startup_commands = dict()
        self.commands = dict()
        self.bulk_mode = False

    def set_bulk_mode(self):
        self.bulk_mode = True

    def send_initial_updates(self):
        commands = self.startup_commands
        self.startup_commands = None
        if commands:
            c = command.DownloaderBatchCommand(RemoteDownloader.dldaemon,
                                               commands)
            c.send()

    def send_updates(self):
        commands = self.commands
        self.commands = dict()
        if commands:
            c = command.DownloaderBatchCommand(RemoteDownloader.dldaemon,
                                               commands)
            c.send()
        elif self.bulk_mode:
            from miro.messages import DownloaderSyncCommandComplete
            # If we did a pause/resume/cancel all, and there weren't any
            # items in the list to send nobody would re-enable the auto-sort.
            # So we do it here.
            DownloaderSyncCommandComplete().send_to_frontend()
        # Reset the bulk mode notification.
        self.bulk_mode = False
        self.start_updates()

    def start_updates(self):
        eventloop.add_timeout(self.UPDATE_INTERVAL,
                              self.send_updates,
                              "Send Download Command Updates")

    def get_download(self, dlid):
        try:
            return self.downloads[dlid]
        except KeyError:
            return None

    def add_download(self, dlid, downloader):
        self.downloads[dlid] = downloader

    def delete_download(self, dlid):
        try:
            del self.downloads[dlid]
        except KeyError:
            return False
        else:
            return True

    def daemon_started(self):
        return self.daemon_starter and self.daemon_starter.started

    def queue(self, identifier, cmd, args):
        if not self.downloads.has_key(identifier):
            raise ValueError('add_download() not called before queue()')

        # Catch restores first, we will flush them when the downloader's
        # started.
        if cmd == self.RESTORE and not self.daemon_started():
            self.startup_commands[identifier] = (cmd, args)
            return

        exists = self.commands.has_key(identifier)

        # Make sure that a pause/resume pair cancel each other out.  For
        # others, assume that a subsequent command can completely supercede
        # the previous command.
        if exists:
            old_cmd, unused = self.commands[identifier]
            if (old_cmd == self.RESUME and cmd == self.PAUSE or
              old_cmd == self.PAUSE and cmd == self.RESUME):
                # Make sure that we unfreeze it
                self.downloads[identifier].status_updates_frozen = False
                del self.commands[identifier]
                return
            # HACK: When we pause and resume we currently send a download
            # command, then a restore downloader command which doesn't
            # do anything.  This also breaks our general assumption that a 
            # current command can completely supercede any previous queued
            # command so if we see it disable it.  I'm not actually
            # sure why we'd want to send a restore command in this case.
            if cmd == self.RESTORE:
               logging.info('not restoring active download')
               return

        # Freeze the status updates, but don't freeze if it is a restore.
        if not cmd == self.RESTORE:
            self.downloads[identifier].status_updates_frozen = True
        self.commands[identifier] = (cmd, args)

    def init_controller(self):
        """Intializes the download daemon controller.

        This doesn't actually start up the downloader daemon, that's done
        in startup_downloader.  Commands will be queued until then.
        """
        self.daemon_starter = DownloadDaemonStarter()

    def startup_downloader(self):
        """Initialize the downloaders.
    
        This method currently does 2 things.  It deletes any stale files
        self in Incomplete Downloads, then it restarts downloads that have
        been restored from the database.  It must be called before any
        RemoteDownloader objects get created.
        """
        self.daemon_starter.startup()
        # Now that the daemon has started, we can process updates.
        self.send_initial_updates()
        self.start_updates()
    
    def shutdown_downloader(self, callback=None):
        if self.daemon_starter:
            self.daemon_starter.shutdown(callback)
        elif callback:
            callback()

def get_downloader_by_dlid(dlid):
    try:
        return RemoteDownloader.get_by_dlid(dlid)
    except ObjectNotFoundError:
        return None

@returns_unicode
def generate_dlid():
    dlid = u"download%08d" % random.randint(0, 99999999)
    while get_downloader_by_dlid(dlid=dlid):
        dlid = u"download%08d" % random.randint(0, 99999999)
    return dlid

class RemoteDownloader(DDBObject):
    """Download a file using the downloader daemon."""
    MIN_STATUS_UPDATE_SPACING = 0.7
    def setup_new(self, url, item, contentType=None, channelName=None):
        check_u(url)
        if contentType:
            check_u(contentType)
        self.origURL = self.url = url
        self.item_list = []
        self.child_deleted = False
        self.main_item_id = None
        self.dlid = generate_dlid()
        self.status = {}
        self.metainfo = None
        self.state = u'downloading'
        if contentType is None:
            # HACK: Some servers report the wrong content-type for
            # torrent files.  We try to work around that by assuming
            # if the enclosure states that something is a torrent,
            # it's a torrent.  Thanks to j@v2v.cc.
            if item.enclosure_type == u'application/x-bittorrent':
                contentType = item.enclosure_type
        self.contentType = u""
        self.delete_files = True
        self.channelName = channelName
        self.manualUpload = False
        self._save_later_dc = None
        self._update_retry_time_dc = None
        self.status_updates_frozen = False
        self.last_update = time.time()
        if contentType is None:
            self.contentType = u""
        else:
            self.contentType = contentType

        if self.contentType == u'':
            self.get_content_type()
        else:
            self.run_downloader()

    @classmethod
    def finished_view(cls):
        return cls.make_view("state in ('finished', 'uploading', "
                             "'uploading-paused')")

    @classmethod
    def auto_uploader_view(cls):
        def check_func(downloader):
            return (downloader.state == 'uploading' and
                    not downloader.manualUpload)
        return cls.make_view("state == 'uploading' AND NOT manualUpload",
                             check_func=check_func)

    @classmethod
    def get_by_dlid(cls, dlid):
        return cls.make_view('dlid=?', (dlid,)).get_singleton()

    @classmethod
    def get_by_url(cls, url):
        return cls.make_view('origURL=?', (url,)).get_singleton()

    @classmethod
    def orphaned_view(cls):
        """Downloaders with no items associated with them."""
        return cls.make_view('id NOT IN (SELECT downloader_id from item)')

    def signal_change(self, needs_save=True, needs_signal_item=True):
        DDBObject.signal_change(self, needs_save=needs_save)
        if needs_signal_item:
            for item in self.item_list:
                item.signal_change(needs_save=False)
        if needs_save:
            self._cancel_save_later()


    def _save_later(self):
        """Save the remote downloader at some point in the future.

        This is used to handle the fact that remote downloaders are
        updated often, but those updates are usually just the status
        dict, which is never used for SELECT statements.  Continually
        saving those changes to disk is just a waste of time and IO.

        Instead, we schedule the save to happen sometime in the
        future.  When miro quits, we call the module-level function
        run_delayed_saves(), which makes sure any pending objects are
        saved to disk.
        """
        if self._save_later_dc is None:
            self._save_later_dc = eventloop.add_timeout(15,
                    self._save_now, "Delayed RemoteDownloader save")

    def _save_now(self):
        """If _save_later() was called and we haven't saved the
        downloader to disk, do it now.
        """
        if self.id_exists() and self._save_later_dc is not None:
            self.signal_change(needs_signal_item=False)

    def _cancel_save_later(self):
        if self._save_later_dc is not None:
            self._save_later_dc.cancel()
            self._save_later_dc = None

    def on_content_type(self, info):
        if not self.id_exists():
            return

        if info['status'] == 200:
            self.url = info['updated-url'].decode('ascii','replace')
            self.contentType = None
            try:
                self.contentType = info['content-type'].decode('ascii',
                                                               'replace')
            except (KeyError, UnicodeDecodeError):
                self.contentType = None
            self.run_downloader()
        else:
            error = httpclient.UnexpectedStatusCode(info['status'])
            self.on_content_type_error(error)

    def on_content_type_error(self, error):
        if not self.id_exists():
            return

        if isinstance(error, httpclient.AuthorizationCanceled):
            # user canceled out of the authorization request, so stop the
            # download.
            self.status['state'] = u'stopped'
            self.signal_change()
            return

        # we can't get a content type.  it's possible that this is a
        # retryable error so we're going to set the contentType to
        # None and run the downloader.  it'll handle HTTP errors
        # better than we will.
        self.contentType = None
        self.run_downloader()

    def get_content_type(self):
        if is_magnet_uri(self.url):
            self.contentType = u'application/x-magnet'
            return 
        httpclient.grab_headers(self.url, self.on_content_type,
                                self.on_content_type_error)

    @classmethod
    def initialize_daemon(cls):
        RemoteDownloader.dldaemon = daemon.ControllerDaemon()

    def _get_rates(self):
        state = self.get_state()
        if state == u'downloading':
            return (self.status.get('rate', 0), self.status.get('upRate', 0))
        if state == u'uploading':
            return (0, self.status.get('upRate', 0))
        return (0, 0)

    def before_changing_status(self):
        rates = self._get_rates()
        app.download_state_manager.total_down_rate -= rates[0]
        app.download_state_manager.total_up_rate -= rates[1]

    def after_changing_status(self):
        self._recalc_state()
        rates = self._get_rates()
        app.download_state_manager.total_down_rate += rates[0]
        app.download_state_manager.total_up_rate += rates[1]

    @classmethod
    def update_status(cls, data, cmd_done=False):
        for field in data:
            if field not in ['filename', 'shortFilename', 'channelName',
                             'metainfo']:
                data[field] = unicodify(data[field])

        self = get_downloader_by_dlid(dlid=data['dlid'])

        if self is not None:
            now = time.time()
            last_update = self.last_update
            rate_limit = False
            state = self.get_state()
            new_state = data.get('state', u'downloading')

            # If this item was marked as pending update, then any update
            # which comes in now which does not have cmd_done set is void.
            if not cmd_done and self.status_updates_frozen:
                logging.debug('self = %s, '
                              'saved state = %s '
                              'downloader state = %s.  '
                              'Discard.',
                              self, state, new_state)
                # treat as stale
                return False

            # If the timing between the status updates is too narrow,
            # try to skip it because it makes the UI jerky otherwise.
            if now < last_update:
                logging.debug('time.time() gone backwards last = %s now = %s',
                              last_update, now)
            else:
                diff = now - last_update
                if diff < self.MIN_STATUS_UPDATE_SPACING:
                    logging.debug('Rate limit: '
                                  'self = %s, now - last_update = %s, '
                                  'MIN_STATUS_UPDATE_SPACING = %s.',
                                  self, diff, self.MIN_STATUS_UPDATE_SPACING)
                    rate_limit = True

            # If the state is one which we set and was meant to be passed
            # through to the downloader (valid_states), and the downloader
            # replied with something that was a response to a previous
            # download command, and state was also a part of valid_states,
            # but the saved state and the new state do not match
            # then it means the message is stale.
            #
            # Have a think about why this is true: when you set a state,
            # which is authoritative, to the downloader you expect it
            # to reply with that same state.  If they do not match then it
            # means the message is stale.
            #
            # The exception to this rule is if the downloader replies with
            # an error state, or if downloading has transitioned to finished
            # state.
            #
            # This also does not apply to any state which we set on the
            # downloader via a restore command.  A restore command before
            # a pause/resume/cancel will work as intended, and no special
            # trickery is required.  A restore command which happens after
            # a pause/resume/cancel is void, so no work is required.
            #
            # I hope this makes sense and is clear!
            valid_states = (u'downloading', u'paused', u'stopped',
                            u'uploading-paused', u'finished')
            if (cmd_done and
              state in valid_states and new_state in valid_states and
              state != new_state):
                if not (state == u'downloading' and new_state == u'finished'):
                    logging.debug('self = %s STALE.  '
                                  'Saved state %s, got state %s.  Discarding.',
                                  self, state, new_state)
                    return False

            # We are updating!  Reset the status_updates_frozen flag.
            self.status_updates_frozen = False
 
            # FIXME - this should get fixed.
            metainfo = data.pop('metainfo', self.metainfo)

            # For metainfo, the downloader process doesn't send the
            # keys if they haven't changed.  Therefore, use our
            # current values if the key isn't present.
            current = (self.status, self.metainfo)
            new = (data, metainfo)
            if current == new:
                return True

            # We have something to update: update the last updated timestamp.
            self.last_update = now

            was_finished = self.is_finished()
            old_filename = self.get_filename()
            self.before_changing_status()

            # FIXME: how do we get all of the possible bit torrent
            # activity strings into gettext? --NN
            if data.has_key('activity') and data['activity']:
                data['activity'] = _(data['activity'])

            # only set attributes if something's changed.  This makes our
            # UPDATE statments contain less data
            if data != self.status:
                self.status = data
            if metainfo != self.metainfo:
                self.metainfo = metainfo
            self._recalc_state()

            # Store the time the download finished
            finished = self.is_finished() and not was_finished
            file_migrated = (self.is_finished() and
                             self.get_filename() != old_filename)
            needs_signal_item = not (finished or file_migrated or rate_limit)
            self.after_changing_status()

            if ((self.get_state() == u'uploading'
                 and not self.manualUpload
                 and (app.config.get(prefs.LIMIT_UPLOAD_RATIO)
                      and self.get_upload_ratio() > app.config.get(prefs.UPLOAD_RATIO)))):
                self.stop_upload()

            if self.changed_attributes == set(('status',)):
                # if we just changed status, then we can wait a while
                # to store things to disk.  Since we go through
                # update_status() often, this results in a fairly
                # large performance gain and alleviates #12101
                self._save_later()
                self.signal_change(needs_signal_item=needs_signal_item,
                                   needs_save=False)
            else:
                self.signal_change()

            if finished:
                for item in self.item_list:
                    item.on_download_finished()
            elif file_migrated:
                self._file_migrated(old_filename)

        return True

    def run_downloader(self):
        """This is the actual download thread.
        """
        flashscraper.try_scraping_url(self.url, self._run_downloader)

    def _run_downloader(self, url, contentType=None, title=None):
        if not self.id_exists():
            # we got deleted while we were doing the flash scraping
            return
        if contentType is not None:
            self.contentType = contentType
        if url is not None:
            if title is not None:
                for mem in self.item_list:
                    if not mem.title:
                        mem.title = title

            self.url = url
            logging.debug("downloading url %s", self.url)
            args = dict(url=self.url, content_type=self.contentType,
                        channel_name=self.channelName)
            app.download_state_manager.add_download(self.dlid, self)
            app.download_state_manager.queue(self.dlid,
                                             app.download_state_manager.RESUME,
                                             args)
            self.status["state"] = u"downloading"
        else:
            self.status["state"] = u'failed'
            self.status["shortReasonFailed"] = _('File not found')
            self.status["reasonFailed"] = _('Flash URL Scraping Error')
        self.signal_change()

    def pause(self):
        """Pauses the download."""
        if app.download_state_manager.get_download(self.dlid):
            args = dict(upload=False)
            app.download_state_manager.queue(self.dlid,
                                             app.download_state_manager.PAUSE,
                                             args)
        self.before_changing_status()
        self.status["state"] = u"paused"
        self.after_changing_status()
        self.signal_change()

    def stop(self, delete):
        """Stops the download and removes the partially downloaded
        file.
        """
        if self.get_state() in [u'downloading', u'uploading', u'paused',
                                u'offline']:
            if app.download_state_manager.get_download(self.dlid):
                args = dict(upload=False, delete=delete)
                app.download_state_manager.queue(
                    self.dlid,
                    app.download_state_manager.STOP,
                    args)
                app.download_state_manager.delete_download(self.dlid)

        if delete:
            self.delete()
        self.status["state"] = u"stopped"
        self.signal_change()

    def delete(self):
        if "filename" in self.status:
            filename = self.status['filename']
        else:
            return
        try:
            fileutil.delete(filename)
        except OSError:
            logging.exception("Error deleting downloaded file: %s",
                              to_uni(filename))

        parent = os.path.join(fileutil.expand_filename(filename),
                              os.path.pardir)
        parent = os.path.normpath(parent)
        movies_dir = fileutil.expand_filename(app.config.get(prefs.MOVIES_DIRECTORY))
        if ((os.path.exists(parent) and os.path.exists(movies_dir)
             and not samefile(parent, movies_dir)
             and len(os.listdir(parent)) == 0)):
            try:
                os.rmdir(parent)
            except OSError:
                logging.exception("Error deleting empty download directory: %s",
                                  to_uni(parent))

    def start(self):
        """Continues a paused, stopped, or failed download thread
        """
        if self.get_state() == u'failed':
            # For failed downloads, don't trust the redirected URL (#14232)
            self.url = self.origURL
            app.download_state_manager.delete_download(self.dlid)
            self.dlid = generate_dlid()
            self.before_changing_status()
            self.status = {}
            self.after_changing_status()
            if self.contentType == u"":
                self.get_content_type()
            else:
                self.run_downloader()
            self.signal_change()
        elif self.get_state() in (u'stopped', u'paused', u'offline'):
            if app.download_state_manager.get_download(self.dlid):
                args = dict(url=self.url, content_type=self.contentType,
                            channel_name=self.channelName)
                app.download_state_manager.queue(
                    self.dlid,
                    app.download_state_manager.RESUME,
                    args)
            self.status['state'] = u'downloading'
            self.restart()
            self.signal_change()

    def migrate(self, directory):
        if app.download_state_manager.get_download(self.dlid):
            c = command.MigrateDownloadCommand(RemoteDownloader.dldaemon,
                                               self.dlid, directory)
            c.send()
        else:
            # downloader doesn't have our dlid.  Move the file ourself.
            short_filename = self.status.get("shortFilename")
            if not short_filename:
                logging.warning(
                    "can't migrate download; no shortfilename!  URL was %s",
                    self.url)
                return
            filename = self.status.get("filename")
            if not filename:
                logging.warning(
                    "can't migrate download; no filename!  URL was %s",
                    self.url)
                return
            if fileutil.exists(filename):
                if self.status.get('channelName', None) is not None:
                    channelName = filter_directory_name(self.status['channelName'])
                    directory = os.path.join(directory, channelName)
                if not os.path.exists(directory):
                    try:
                        fileutil.makedirs(directory)
                    except OSError:
                        # FIXME - what about permission issues?
                        pass
                newfilename = os.path.join(directory, short_filename)
                if newfilename == filename:
                    return
                # create a file or directory to serve as a placeholder before
                # we start to migrate.  This helps ensure that the destination
                # we're migrating too is not already taken.
                try:
                    is_dir = fileutil.isdir(filename)
                    if is_dir:
                        newfilename = next_free_directory(newfilename)
                        fp = None
                    else:
                        newfilename, fp = next_free_filename(newfilename)
                        fp.close()
                except ValueError:
                    func = ('next_free_directory' if is_dir
                            else 'next_free_filename')
                    logging.warn('migrate: %s failed.  candidate = %r',
                                 func, newfilename)
                else:
                    def callback():
                        self.status['filename'] = newfilename
                        self.signal_change(needs_signal_item=False)
                        self._file_migrated(filename)
                    fileutil.migrate_file(filename, newfilename, callback)
        for i in self.item_list:
            i.migrate_children(directory)

    def _file_migrated(self, old_filename):
        # Make sure that item_list is populated with items, see (#12202)
        for item in models.Item.downloader_view(self.id):
            self.add_item(item)
        for item in self.item_list:
            item.on_downloader_migrated(old_filename, self.get_filename())

    def set_delete_files(self, delete_files):
        self.delete_files = delete_files

    def set_channel_name(self, channelName):
        if self.channelName is None:
            if channelName:
                check_f(channelName)
            self.channelName = channelName

    def remove(self):
        """Removes downloader from the database and deletes the file.
        """
        rates = self._get_rates()
        app.download_state_manager.total_down_rate -= rates[0]
        app.download_state_manager.total_up_rate -= rates[1]
        self.stop(self.delete_files)
        DDBObject.remove(self)

    def get_type(self):
        """Get the type of download.  Will return either "http" or
        "bittorrent".
        """
        self.confirm_db_thread()
        if ((self.contentType == u'application/x-bittorrent'
             or self.contentType == u'application/x-magnet')):
            return u"bittorrent"

        return u"http"

    def add_item(self, item):
        """In case multiple downloaders are getting the same file, we
        can support multiple items
        """
        if item not in self.item_list:
            self.item_list.append(item)
            if self.main_item_id is None:
                self.main_item_id = item.id
                self.signal_change()

    def remove_item(self, item):
        self.item_list.remove(item)
        if len (self.item_list) == 0:
            self.remove()
        elif item.id == self.main_item_id:
            self.main_item_id = self.item_list[0].id
            self.signal_change()

    def get_rate(self):
        self.confirm_db_thread()
        return self.status.get('rate', 0)

    def get_eta(self):
        self.confirm_db_thread()
        return self.status.get('eta', 0)

    @returns_unicode
    def get_startup_activity(self):
        self.confirm_db_thread()
        activity = self.status.get('activity')
        if ((activity is None and self.status.get('retryCount', -1) > -1
             and 'retryTime' in self.status)):
            activity = self._calc_retry_time()
            if self._update_retry_time_dc is None:
                self._update_retry_time_dc = eventloop.add_timeout(1,
                        self._update_retry_time, 'Updating retry time')
        if activity is None:
            return _("starting up")
        return activity

    def _calc_retry_time(self):
        if self.status['retryTime'] > datetime.datetime.now():
            retry_delta = self.status['retryTime'] - datetime.datetime.now()
            time_str = displaytext.time_string(retry_delta.seconds)
            return _('no connection - retrying in %(time)s', {"time": time_str})
        else:
            return _('no connection - retrying soon')

    def _update_retry_time(self):
        if self.id_exists():
            # calling signal_change() will cause the us to call
            # get_startup_activity() again which will have a new time now.
            self.signal_change(needs_save=False)
            self._update_retry_time_dc = None

    def _cancel_retry_time_update(self):
        if self._update_retry_time_dc:
            self._update_retry_time_dc.cancel()
            self._update_retry_time_dc = None

    @returns_unicode
    def get_reason_failed(self):
        """Returns the reason for the failure of this download.  This
        should only be called when the download is in the failed
        state.
        """
        if not self.get_state() == u'failed':
            msg = u"get_reason_failed() called on a non-failed downloader"
            raise ValueError(msg)
        self.confirm_db_thread()
        return self.status.get('reasonFailed', _("Unknown"))

    @returns_unicode
    def get_short_reason_failed(self):
        if not self.get_state() == u'failed':
            msg = u"get_short_reason_failed() called on a non-failed downloader"
            raise ValueError(msg)
        self.confirm_db_thread()
        return self.status.get('shortReasonFailed', _("Unknown"))

    @returns_unicode
    def get_url(self):
        """Returns the URL we're downloading
        """
        self.confirm_db_thread()
        return self.url

    @returns_unicode
    def get_state(self):
        """Returns the state of the download: downloading, paused,
        stopped, failed, or finished.
        """
        self.confirm_db_thread()
        return self.state

    def is_finished(self):
        return self.get_state() in (u'finished', u'uploading',
                                    u'uploading-paused')

    def get_total_size(self):
        """Returns the total size of the download in bytes.
        """
        self.confirm_db_thread()
        return self.status.get('totalSize', -1)

    def get_current_size(self):
        """Returns the current amount downloaded in bytes.
        """
        self.confirm_db_thread()
        return self.status.get('currentSize', 0)

    @returns_filename
    def get_filename(self):
        """Returns the filename that we're downloading to.  Should not be
        called until state is "finished."
        """
        self.confirm_db_thread()
        # FIXME - FilenameType('') is a bogus value, but looks like a
        # filename.  should return None.
        return self.status.get('filename', FilenameType(''))

    def setup_restored(self):
        self.status_updates_frozen = False
        self.last_update = time.time()
        self._save_later_dc = None
        self._update_retry_time_dc = None
        self.delete_files = True
        self.item_list = []
        if self.dlid == 'noid':
            # this won't happen nowadays, but it can for old databases
            self.dlid = generate_dlid()
        self.status['rate'] = 0
        self.status['upRate'] = 0
        self.status['eta'] = 0

    def on_signal_change(self):
        self._recalc_state()

    def _recalc_state(self):
        new_state = self.status.get('state', u'downloading')
        # avoid altering changed_attributes if we don't need to
        if new_state != self.state:
            self.state = new_state

    def get_upload_ratio(self):
        size = self.get_current_size()
        if size == 0:
            return 0
        return self.status.get('uploaded', 0) / size

    def restart_on_startup_if_needed(self):
        if not self.id_exists():
            return
        if app.download_state_manager.get_download(self.dlid):
            # something has caused us to restart already, (for
            # example, the user selects "resume seeding").  squelch
            # any automatic behaviour (#12462)
            return
        if self.get_state() in (u'downloading', u'offline'):
            self.restart()
        if self.get_state() == u'uploading':
            if ((self.manualUpload
                 or (app.config.get(prefs.LIMIT_UPLOAD_RATIO)
                     and self.get_upload_ratio() < app.config.get(prefs.UPLOAD_RATIO)))):
                self.restart()
            else:
                self.stop_upload()

    def restart(self):
        if not self.status or self.status.get('dlerType') is None:
            if self.contentType == u"":
                self.get_content_type()
            else:
                self.run_downloader()
        else:
            app.download_state_manager.add_download(self.dlid, self)
            dler_status = self.status
            # FIXME: not sure why this is necessary
            if self.contentType ==  u'application/x-magnet':
                dler_status['url'] = self.url
            dler_status['metainfo'] = self.metainfo
            args = dict(downloader=dler_status)
            app.download_state_manager.queue(
                self.dlid,
                app.download_state_manager.RESTORE,
                args)
            self.before_changing_status()
            self.status['state'] = u'downloading'
            self.after_changing_status()

    def start_upload(self):
        """
        Start an upload (seeding).
        """
        if self.get_type() != u'bittorrent':
            logging.warn("called start_upload for non-bittorrent downloader")
            return
        if self.child_deleted:
            title = "Can't Resume Seeding"
            msg = ("Seeding cannot resume because part of this torrent "
                   "has been deleted.")
            dialogs.MessageBoxDialog(title, msg).run()
            return
        if self.get_state() not in (u'finished', u'uploading-paused'):
            logging.warn("called start_upload when downloader state is: %s",
                         self.get_state())
            return
        self.manualUpload = True
        if app.download_state_manager.get_download(self.dlid):
            args = dict(url=self.url, content_type=self.contentType,
                        channel_name=self.channelName)
            app.download_state_manager.queue(self.dlid,
                                             app.download_state_manager.RESUME,
                                             args)
        else:
            self.before_changing_status()
            self.status['state'] = u'uploading'
            self.after_changing_status()
            self.restart()
            self.signal_change()

    def stop_upload(self):
        """
        Stop uploading/seeding and set status as "finished".
        """
        if app.download_state_manager.get_download(self.dlid):
            args = dict(upload=True)
            app.download_state_manager.queue(self.dlid,
                app.download_state_manager.STOP, args)
            app.download_state_manager.delete_download(self.dlid)
        self.before_changing_status()
        self.status["state"] = u"finished"
        self.after_changing_status()
        self.signal_change()

    def pause_upload(self):
        """
        Stop uploading/seeding and set status as "uploading-paused".
        """
        if app.download_state_manager.get_download(self.dlid):
            args = dict(upload=True)
            app.download_state_manager.queue(self.dlid,
                app.download_state_manager.PAUSE,
                args)
            app.download_state_manager.delete_download(self.dlid)
        self.before_changing_status()
        self.status["state"] = u"uploading-paused"
        self.after_changing_status()
        self.signal_change()

def cleanup_incomplete_downloads():
    download_dir = os.path.join(app.config.get(prefs.MOVIES_DIRECTORY),
                                'Incomplete Downloads')
    if not fileutil.exists(download_dir):
        return

    files_in_use = set()
    for downloader in RemoteDownloader.make_view():
        if downloader.get_state() in ('downloading', 'paused',
                                     'offline', 'uploading', 'finished',
                                     'uploading-paused'):
            filename = downloader.get_filename()
            if len(filename) > 0:
                if not fileutil.isabs(filename):
                    filename = os.path.join(download_dir, filename)
                files_in_use.add(filename)

    try:
        entries = fileutil.listdir(download_dir)
    except OSError:
        entries = []

    for f in entries:
        f = os.path.join(download_dir, f)
        if f not in files_in_use:
            try:
                if fileutil.isfile(f):
                    fileutil.remove(f)
                elif fileutil.isdir(f):
                    fileutil.rmtree(f)
            except OSError:
                # FIXME - maybe a permissions error?
                pass

def kill_uploaders(*args):
    torrent_limit = app.config.get(prefs.UPSTREAM_TORRENT_LIMIT)
    auto_uploads = list(RemoteDownloader.auto_uploader_view())
    for dler in auto_uploads[torrent_limit:]:
        dler.stop_upload()

def _on_config_change(obj, key, value):
    if key == prefs.UPSTREAM_TORRENT_LIMIT.key:
        kill_uploaders()


class DownloadDaemonStarter(object):
    def __init__(self):
        RemoteDownloader.initialize_daemon()
        self.downloads_at_startup = list(RemoteDownloader.make_view())
        self.started = False
        self._config_callback_handle = None
        self._download_tracker = None

    def limit_uploaders(self):
        view = RemoteDownloader.auto_uploader_view()
        self._download_tracker = view.make_tracker()
        self._download_tracker.connect('added', kill_uploaders)
        self._config_callback_handle = app.backend_config_watcher.connect(
                "changed", _on_config_change)
        kill_uploaders()

    def disconnect_signals(self):
        if self._download_tracker is not None:
            self._download_tracker.unlink()
            self._download_tracker = None
        if self._config_callback_handle is not None:
            app.backend_config_watcher.disconnect(self._config_callback_handle)
            self._config_callback_handle = None

    def startup(self):
        cleanup_incomplete_downloads()
        RemoteDownloader.dldaemon.start_downloader_daemon()
        self.limit_uploaders()
        self.restart_downloads()
        self.started = True

    def restart_downloads(self):
        for downloader in self.downloads_at_startup:
            downloader.restart_on_startup_if_needed()

    def shutdown(self, callback):
        self.disconnect_signals()
        self.shutdown_callback = callback
        if not self.started:
            self._on_shutdown()
        else:
            RemoteDownloader.dldaemon.shutdown_downloader_daemon(
                    callback=self._on_shutdown)

    def _on_shutdown(self):
        shutdown_downloader_objects()
        self.shutdown_callback()
        del self.shutdown_callback

def lookup_downloader(url):
    try:
        return RemoteDownloader.get_by_url(url)
    except ObjectNotFoundError:
        return None

def get_existing_downloader_by_url(url):
    downloader = lookup_downloader(url)
    return downloader

def get_existing_downloader(item):
    try:
        return RemoteDownloader.get_by_id(item.downloader_id)
    except ObjectNotFoundError:
        return None

def get_downloader_for_item(item):
    existing = get_existing_downloader(item)
    if existing:
        return existing
    url = item.get_url()
    existing = get_existing_downloader_by_url(url)
    if existing:
        return existing
    channelName = unicode_to_filename(item.get_channel_title(True))
    if not channelName:
        channelName = None
    if url.startswith(u'file://'):
        path = get_file_url_path(url)
        try:
            get_torrent_info_hash(path)
        except ValueError:
            raise ValueError("Don't know how to handle %s" % url)
        except (OSError, IOError):
            return None
        else:
            return RemoteDownloader(url, item, u'application/x-bittorrent',
                                    channelName=channelName)
    elif is_magnet_uri(url):
        return RemoteDownloader(url, item, u'application/x-magnet')
    else:
        return RemoteDownloader(url, item, channelName=channelName)

def shutdown_downloader_objects():
    """Perform shutdown code for RemoteDownloaders.

    This means a couple things:
      - Make sure any RemoteDownloaders with pending changes get saved.
      - Cancel the update retry time callbacks
    """
    for downloader in RemoteDownloader.make_view():
        downloader._save_now()
        downloader._cancel_retry_time_update()
