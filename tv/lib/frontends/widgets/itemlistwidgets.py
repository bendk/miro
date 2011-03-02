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

"""itemlistwidgets.py -- Widgets to display lists of items

itemlist, itemlistcontroller and itemlistwidgets work together using
the MVC pattern.  itemlist handles the Model, itemlistwidgets handles
the View and itemlistcontroller handles the Controller.

The classes inside this module are meant to be as dumb as possible.
They should only worry themselves about how things are displayed.  The
only thing they do in response to user input or other signals is to
forward those signals on.  It's the job of ItemListController
subclasses to handle the logic involved.
"""

from miro import app
from miro import prefs
from miro import displaytext
from miro import util
from miro import eventloop
from miro.gtcache import gettext as _
from miro.gtcache import declarify
from miro.frontends.widgets import imagepool
from miro.frontends.widgets import style
from miro.frontends.widgets import widgetconst
from miro.frontends.widgets import widgetutil
from miro.frontends.widgets import segmented
from miro.frontends.widgets import separator
from miro.frontends.widgets.widgetstatestore import WidgetStateStore
from miro.plat import resources
from miro.plat.frontends.widgets import widgetset
from miro.plat.utils import get_available_bytes_for_movies

class ViewToggler(widgetset.CustomButton):
    def __init__(self):
        widgetset.CustomButton.__init__(self)
        self.selected_view = WidgetStateStore.get_standard_view_type()
        self.normal_image = imagepool.get_surface(resources.path(
            'images/normal-view-button-icon.png'))
        self.list_image = imagepool.get_surface(resources.path(
            'images/list-view-button-icon.png'))
        self.connect('clicked', self._on_clicked)
        self.create_signal('normal-view-clicked')
        self.create_signal('list-view-clicked')

    def size_request(self, layout):
        return self.normal_image.width, 50 # want to make the titlebar higher

    def draw(self, context, layout):
        if WidgetStateStore.is_standard_view(self.selected_view):
            image = self.normal_image
        else:
            image = self.list_image
        y = int((context.height - image.height) / 2)
        image.draw(context, 0, y, image.width, image.height)

    def switch_to_view(self, view):
        if view is not self.selected_view:
            self.selected_view = view
            self.queue_redraw()

    def _on_clicked(self, button):
        if WidgetStateStore.is_standard_view(self.selected_view):
            self.emit('list-view-clicked')
            self.switch_to_view(WidgetStateStore.get_list_view_type())
        else:
            self.emit('normal-view-clicked')
            self.switch_to_view(WidgetStateStore.get_standard_view_type())

class FilterButton(widgetset.CustomButton):

    SURFACE = widgetutil.ThreeImageSurface('filter')
    TEXT_SIZE = 0.75
    ON_COLOR = (1, 1, 1)
    OFF_COLOR = (0.247, 0.247, 0.247)

    def __init__(self, text, enabled=False):
        self.text = text
        self.enabled = enabled
        widgetset.CustomButton.__init__(self)
        self.connect('clicked', self._on_clicked)

    def _textbox(self, layout):
        layout.set_font(self.TEXT_SIZE)
        return layout.textbox(self.text)

    def size_request(self, layout):
        width, height = self._textbox(layout).get_size()
        return width + 20, max(self.SURFACE.height, height)

    def draw(self, context, layout):
        surface_y = (context.height - self.SURFACE.height) / 2
        if self.enabled:
            self.SURFACE.draw(context, 0, surface_y, context.width)
            layout.set_text_color(self.ON_COLOR)
        else:
            layout.set_text_color(self.OFF_COLOR)
        textbox = self._textbox(layout)
        text_width, text_height = textbox.get_size()
        text_x = (context.width - text_width) / 2
        text_y = (context.height - text_height) / 2
        textbox.draw(context, text_x, text_y, context.width, context.height)

    def set_enabled(self, enabled):
        if enabled != self.enabled:
            self.enabled = enabled
            self.queue_redraw()

    def _on_clicked(self, button):
        self.set_enabled(not self.enabled)

class BoxedIconDrawer(widgetset.DrawingArea):
    """Draws the icon for an item list."""
    def __init__(self, image):
        widgetset.DrawingArea.__init__(self)
        self.icon = widgetset.ImageSurface(image)

    def size_request(self, layout):
        return (41, 41)

    def draw(self, context, layout):
        widgetutil.draw_rounded_icon(context, self.icon, 0, 0, 41, 41,
                                     inset=1)
        context.set_line_width(1)
        # Draw the black inner border
        context.set_color((0, 0, 0), 0.16)
        widgetutil.round_rect(context, 1.5, 1.5, 38, 38, 3)
        context.stroke()
        # Draw the white outer border
        context.set_color((1, 1, 1), 0.76)
        widgetutil.round_rect(context, 0.5, 0.5, 40, 40, 3)
        context.stroke()

class ItemListTitlebar(widgetset.Background):
    """Titlebar for feeds, playlists and static tabs that display
    items.

    :signal list-view-clicked: (widget) User requested to switch to
        list view
    :signal normal-view-clicked: (widget) User requested to switch to
        normal view
    :signal search-changed: (self, search_text) -- The value in the
        search box changed and the items listed should be filtered
    """
    def __init__(self):
        widgetset.Background.__init__(self)
        self.create_signal('resume-playing')
        hbox = widgetset.HBox()
        self.add(hbox)
        # Pack stuff to the right
        start = self._build_titlebar_start()
        if start:
            hbox.pack_start(start)
        self.filter_box = widgetset.HBox(spacing=10)
        hbox.pack_start(self.filter_box)
        extra = self._build_titlebar_extra()
        if extra:
            if isinstance(extra, list):
                [hbox.pack_end(w) for w in extra[::-1]]
            else:
                hbox.pack_end(extra)
        hbox.pack_end(self._build_view_toggle())
        self.resume_button = widgetset.Button(_("Resume foo at x:yz"))
        self.resume_button.connect('clicked', self._on_resume_button_clicked)
        self.resume_button_holder = widgetutil.HideableWidget(
                widgetutil.pad(self.resume_button, right=10))
        hbox.pack_end(widgetutil.align_middle(self.resume_button_holder))

        self.filters = {}

    def draw(self, context, layout):
        if not context.style.use_custom_titlebar_background:
            return
        context.move_to(0, 0)
        context.rel_line_to(context.width, 0)
        context.set_color((224.0 / 255, 224.0 / 255, 224.0 / 255))
        context.stroke()
        gradient = widgetset.Gradient(0, 1, 0, context.height)
        gradient.set_start_color((212.0 / 255, 212.0 / 255, 212.0 / 255))
        gradient.set_end_color((168.0 / 255, 168.0 / 255, 168.0 / 255))
        context.rectangle(0, 1, context.width, context.height)
        context.gradient_fill(gradient)

    def update_resume_button(self, text):
        """Update the resume button text.

        If text is None, we will hide the resume button.  Otherwise we
        will show the button and have it display text.
        """
        if text is None:
            self.resume_button_holder.hide()
        else:
            self.resume_button.set_text(text)
            self.resume_button_holder.show()

    def _build_titlebar_start(self):
        """Builds the widgets to place at the start of the titlebar.
        """

    def _build_titlebar_extra(self):
        """Builds the widget(s) to place to the right of the title.

        By default we add a search box, but subclasses can override
        this.
        """
        self.create_signal('search-changed')
        self.searchbox = widgetset.SearchTextEntry()
        self.searchbox.connect('changed', self._on_search_changed)
        return widgetutil.align_middle(self.searchbox, right_pad=35,
                                       left_pad=15)

    def _build_view_toggle(self):
        self.create_signal('list-view-clicked')
        self.create_signal('normal-view-clicked')
        self.view_toggler = ViewToggler()
        self.view_toggler.connect('list-view-clicked', self._on_list_clicked)
        self.view_toggler.connect('normal-view-clicked',
                                  self._on_normal_clicked)
        return self.view_toggler

    def _on_resume_button_clicked(self, button):
        self.emit('resume-playing')

    def _on_search_changed(self, searchbox):
        self.emit('search-changed', searchbox.get_text())

    def _on_normal_clicked(self, button):
        self.emit('normal-view-clicked')

    def _on_list_clicked(self, button):
        self.emit('list-view-clicked')

    def switch_to_view(self, view):
        self.view_toggler.switch_to_view(view)

    def set_title(self, title):
        self.title_drawer = title
        self.title_drawer.queue_redraw()

    def set_search_text(self, text):
        self.searchbox.set_text(text)

    def toggle_filter(self, filter):
        # implemented by subclasses
        pass

    def add_filter(self, name, signal_name, signal_param, label):
        if not self.filters:
            enabled = True
        else:
            enabled = False
        self.create_signal(signal_name)
        def callback(button):
            self.emit(signal_name, signal_param)
        button = FilterButton(label, enabled=enabled)
        button.connect('clicked', callback)
        self.filter_box.pack_start(button)
        self.filters[name] = button
        return button

class SearchTitlebar(ItemListTitlebar):
    """
    Titlebar for views which can save their view as a podcast.

    :signal save-search: (self, search_text) The current search
        should be saved as a search channel.
    """
    def _build_titlebar_start(self):
        self.create_signal('save-search')
        button = widgetset.Button(_('Save as Podcast'), style="smooth")
        button.connect('clicked', self._on_save_search)
        self.save_button = widgetutil.HideableWidget(
                widgetutil.pad(button, right=20))
        return widgetutil.align_middle(self.save_button, left_pad=20)

    def _on_save_search(self, button):
        self.emit('save-search', self.searchbox.get_text())

    def _on_search_changed(self, searchbox):
        if searchbox.get_text() == '':
            self.save_button.hide()
        else:
            self.save_button.show()
        self.emit('search-changed', searchbox.get_text())

class FilteredTitlebar(ItemListTitlebar):
    def __init__(self):
        ItemListTitlebar.__init__(self)
        # this "All" is different than other "All"s in the codebase, so it
        # needs to be clarified
        view_all = WidgetStateStore.get_view_all_filter()
        unwatched = WidgetStateStore.get_unwatched_filter()
        downloaded = WidgetStateStore.get_downloaded_filter()
        self.add_filter('view-all', 'toggle-filter', view_all,
                         declarify(_('View|All')))
        self.add_filter('only-downloaded', 'toggle-filter', downloaded,
                        _('Downloaded'))
        self.add_filter('only-unplayed', 'toggle-filter', unwatched,
                        _('Unplayed'))

    def toggle_filter(self, filter):
        view_all = WidgetStateStore.is_view_all_filter(filter)
        downloaded = WidgetStateStore.has_downloaded_filter(filter)
        unwatched = WidgetStateStore.has_unwatched_filter(filter)
        self.filters['view-all'].set_enabled(view_all)
        self.filters['only-downloaded'].set_enabled(downloaded)
        self.filters['only-unplayed'].set_enabled(unwatched)

class ChannelTitlebar(SearchTitlebar, FilteredTitlebar):
    """Titlebar for a channel
    """

class SearchListTitlebar(SearchTitlebar):
    """Titlebar for the search page.
    """
    def _on_search_activate(self, obj):
        app.search_manager.set_search_info(
            obj.selected_engine(), obj.get_text())
        app.search_manager.perform_search()

    def get_engine(self):
        return self.searchbox.selected_engine()

    def get_text(self):
        return self.searchbox.get_text()

    def set_search_engine(self, engine):
        self.searchbox.select_engine(engine)

    def _build_titlebar_extra(self):
        hbox = widgetset.HBox()
        self.create_signal('search-changed')
        self.searchbox = widgetset.VideoSearchTextEntry()
        w, h = self.searchbox.get_size_request()
        self.searchbox.set_size_request(200, h)
        self.searchbox.connect('validate', self._on_search_activate)
        self.searchbox.connect('changed', self._on_search_changed)
        hbox.pack_start(widgetutil.align_middle(self.searchbox, 0, 0, 16, 16))

        return [widgetutil.align_middle(hbox, right_pad=20)]

class ItemView(widgetset.TableView):
    """TableView that displays a list of items."""
    def __init__(self, item_list, scroll_pos, selection):
        widgetset.TableView.__init__(self, item_list.model)

        self.item_list = item_list
        self.set_fixed_height(True)
        self.allow_multiple_select(True)

        self.create_signal('scroll-position-changed')
        self.scroll_pos = scroll_pos
        self.set_scroll_position(scroll_pos)

        if selection is not None:
            self.set_selection_as_strings(selection)

    def on_undisplay(self):
        self.scroll_pos = self.get_scroll_position()
        if self.scroll_pos is not None:
            self.emit('scroll-position-changed', self.scroll_pos)

class SorterWidgetOwner(object):
    """Mixin for objects that need to handle a set of
    ascending/descending sort indicators.
    """
    def __init__(self):
        self.create_signal('sort-changed')

    def on_sorter_clicked(self, widget, sort_key):
        ascending = not (widget.get_sort_indicator_visible() and
                widget.get_sort_order_ascending())
        self.emit('sort-changed', sort_key, ascending)

    def change_sort_indicator(self, sort_key, ascending):
        for widget_sort_key, widget in self.sorter_widget_map.iteritems():
            if widget_sort_key == sort_key:
                widget.set_sort_order(ascending)
                widget.set_sort_indicator_visible(True)
            else:
                widget.set_sort_indicator_visible(False)

class StandardView(ItemView):
    """TableView that displays a list of items using the standard
    view.
    """

    draws_selection = True

    def __init__(self, item_list, scroll_pos, selection, display_channel=True):
        ItemView.__init__(self, item_list, scroll_pos, selection)
        self.display_channel = display_channel
        self.set_draws_selection(False)
        self.renderer = self.build_renderer()
        self.renderer.total_width = -1
        self.column = widgetset.TableColumn('item', self.renderer)
        self.set_column_spacing(0)
        self.column.set_min_width(self.renderer.MIN_WIDTH)
        self.add_column(self.column)
        self.set_show_headers(False)
        self.set_auto_resizes(True)
        self.set_background_color(widgetutil.WHITE)

    def build_renderer(self):
        return style.ItemRenderer(self.display_channel)

class ListView(ItemView, SorterWidgetOwner):
    """TableView that displays a list of items using the list view."""
    COLUMN_RENDERERS = {
        'state': style.StateCircleRenderer,
        'name': style.NameRenderer,
        'artist': style.ArtistRenderer,
        'album': style.AlbumRenderer,
        'track': style.TrackRenderer,
        'year': style.YearRenderer,
        'genre': style.GenreRenderer,
        'rating': style.RatingRenderer,
        'date': style.DateRenderer,
        'length': style.LengthRenderer,
        'status': style.StatusRenderer,
        'size': style.SizeRenderer,
        'feed-name': style.FeedNameRenderer,
        'eta': style.ETARenderer,
        'torrent-details': style.TorrentDetailsRenderer,
        'rate': style.DownloadRateRenderer,
        'date-added': style.DateAddedRenderer,
        'last-played': style.LastPlayedRenderer,
        'description': style.DescriptionRenderer,
        'drm': style.DRMRenderer,
        'file-type': style.FileTypeRenderer,
        'show': style.ShowRenderer,
    }
    COLUMN_PADDING = 12
    def __init__(self, item_list,
            columns_enabled, column_widths, scroll_pos, selection):
        ItemView.__init__(self, item_list, scroll_pos, selection)
        SorterWidgetOwner.__init__(self)
        self.column_widths = {}
        self.create_signal('columns-enabled-changed')
        self.create_signal('column-widths-changed')
        self._column_name_to_column = {}
        self.sorter_widget_map = self._column_name_to_column
        self._column_by_label = {}
        self._real_column_widths = {}
        self.columns_enabled = []
        self.set_show_headers(True)
        self.set_columns_draggable(True)
        self.set_column_spacing(self.COLUMN_PADDING)
        self.set_row_spacing(5)
        self.set_grid_lines(False, False)
        self.set_alternate_row_backgrounds(True)
        self.html_stripper = util.HTMLStripper()
        self.update_columns(columns_enabled, column_widths)

    def _get_ui_state(self):
        if not self._set_initial_widths:
            return
        enabled = []
        widths = {}
        for label in self.get_columns():
            name = self._column_by_label[label]
            enabled.append(name)
            column = self._column_name_to_column[name]
            width = int(column.get_width())
            if width != self._real_column_widths[name]:
                widths[name] = width
        self.columns_enabled = enabled
        self._real_column_widths.update(widths)
        self.column_widths.update(widths)

    def on_undisplay(self):
        self._get_ui_state()
        ItemView.on_undisplay(self)
        self.emit('column-widths-changed', self.column_widths)
        self.emit('columns-enabled-changed', self.columns_enabled)

    def get_tooltip(self, iter_, column):
        if ('name' in self._column_name_to_column and
                self._column_name_to_column['name'] == column):
            info = self.item_list.model[iter_][0]
            text, links = self.html_stripper.strip(info.description)
            if text:
                if len(text) > 1000:
                    text = text[:994] + ' [...]'
                return text

        elif ('state' in self._column_name_to_column and
                self._column_name_to_column['state'] is column):
            info = self.item_list.model[iter_][0]
            # this logic is replicated in style.StateCircleRenderer
            # with text from style.StatusRenderer
            if info.state == 'downloading':
                return _("Downloading")
            elif (info.downloaded and info.is_playable
                  and not info.video_watched):
                return _("Unplayed")
            elif (not info.item_viewed and not info.expiration_date
                  and not info.is_external):
                return _("Newly Available")
        return None

    def update_columns(self, new_columns, new_widths):
        assert set(new_columns).issubset(new_widths)
        old_columns = set(self.columns_enabled)
        self.columns_enabled = new_columns
        self.column_widths = new_widths
        for name in sorted(set(new_columns) - old_columns,
                key=new_columns.index):
            resizable = not name in widgetconst.NO_RESIZE_COLUMNS
            pad = not name in widgetconst.NO_PAD_COLUMNS
            if name == 'state':
                header = u''
            else:
                header = widgetconst.COLUMN_LABELS[name]
            renderer = ListView.COLUMN_RENDERERS[name]()
            self._make_column(header, renderer, name, resizable, pad)
            self._column_by_label[header] = name
        for name in old_columns - set(new_columns):
            column = self._column_name_to_column[name]
            index = self.columns.index(column)
            self.remove_column(index)
            del self._column_name_to_column[name]
        self._set_initial_widths = False

    def _make_column(self, header, renderer, column_name, resizable=True,
            pad=True):
        column = widgetset.TableColumn(header, renderer)
        column.set_min_width(renderer.min_width)
        if resizable:
            column.set_resizable(True)
        if not pad:
            column.set_do_horizontal_padding(pad)
        if hasattr(renderer, 'right_aligned') and renderer.right_aligned:
            column.set_right_aligned(True)
        if column_name in widgetconst.NO_RESIZE_COLUMNS:
            self.column_widths[column_name] = renderer.min_width
            if pad:
                self.column_widths[column_name] += self.COLUMN_PADDING
            column.set_width(renderer.min_width)
        column.connect_weak('clicked', self.on_sorter_clicked, column_name)
        self._column_name_to_column[column_name] = column
        self.add_column(column)

    def do_size_allocated(self, total_width, height):
        if not self._set_initial_widths:
            self._set_initial_widths = True

            total_weight = 0
            min_width = 0
            for name in self.columns_enabled:
                total_weight += widgetconst.COLUMN_WIDTH_WEIGHTS.get(name, 0)
                min_width += self.column_widths[name]
            if total_weight is 0:
                total_weight = 1

            available_width = self.width_for_columns(total_width)
            extra_width = available_width - min_width

            diff = 0 # prevent cumulative rounding errors
            for name in self.columns_enabled:
                weight = widgetconst.COLUMN_WIDTH_WEIGHTS.get(name, 0)
                extra = extra_width * weight / total_weight + diff
                diff = extra - int(extra)
                width = self.column_widths[name]
                width += int(extra)
                column = self._column_name_to_column[name]
                column.set_width(width)
                self._real_column_widths[name] = int(column.get_width())

class HideableSection(widgetutil.HideableWidget):
    """Widget that contains an ItemView, along with an expander to
    show/hide it.

    The label for a HideableSection expander is made up of 2 parts.
    The header is displayed first using a bold text, then the info is
    displayed using normal font.
    """

    def __init__(self, header_text, item_view):
        self.expander = widgetset.Expander(item_view)
        self.expander.set_expanded(False)
        widget = widgetutil.pad(self.expander, top=3, bottom=3, left=5)
        self._make_label(header_text)
        widgetutil.HideableWidget.__init__(self, widget)

    def set_info(self, text):
        self.info_label.set_text(text)

    def set_header(self, text):
        self.header_label.set_text(text)

    def expand(self):
        self.expander.set_expanded(True)

    def _make_label(self, header_text):
        hbox = widgetset.HBox()
        self.header_label = widgetset.Label(header_text)
        self.header_label.set_size(0.85)
        self.header_label.set_bold(True)
        self.header_label.set_color((0.27, 0.27, 0.27))
        hbox.pack_start(self.header_label)
        self.info_label = widgetset.Label("")
        self.info_label.set_size(0.85)
        self.info_label.set_color((0.72, 0.72, 0.72))
        hbox.pack_start(widgetutil.pad(self.info_label, left=7))
        self.expander.set_label(hbox)

class DisplayToolbar(widgetset.Background):
    def draw(self, context, layout):
        if not context.style.use_custom_titlebar_background:
            return
        # gradient = widgetset.Gradient(0, 0, 0, context.height)
        # gradient.set_start_color((0.90, 0.90, 0.90))
        # gradient.set_end_color((0.79, 0.79, 0.79))
        # context.rectangle(0, 0, context.width, context.height)
        # context.gradient_fill(gradient)

class DownloadStatusToolbar(DisplayToolbar):
    """Widget that shows free space and download and upload speed
    status.
    """

    def __init__(self):
        DisplayToolbar.__init__(self)

        v = widgetset.VBox()

        sep = separator.HSeparator((0.85, 0.85, 0.85), (0.95, 0.95, 0.95))
        v.pack_start(sep)

        h = widgetset.HBox(spacing=5)

        self._free_disk_label = widgetset.Label("")
        self._free_disk_label.set_size(widgetconst.SIZE_SMALL)

        h.pack_start(widgetutil.align_left(self._free_disk_label,
                     top_pad=10, bottom_pad=10, left_pad=20), expand=True)


        # Sigh.  We want to fix these sizes so they don't jump about
        # so reserve the maximum size for these things.  The upload
        # and download are both the same so we only need to
        # auto-detect for one.
        placeholder_bps = 1000 * 1024    # 1000 kb/s - not rounded 1 MB/s yet
        text_up = _("%(rate)s",
                    {"rate": displaytext.download_rate(placeholder_bps)})

        first_label = widgetset.Label("")
        first_label.set_size(widgetconst.SIZE_SMALL)

        # Now, auto-detect the size required.
        first_label.set_text(text_up)
        width, height = first_label.get_size_request()

        first_image = widgetutil.HideableWidget(widgetset.ImageDisplay(
                          widgetset.Image(resources.path('images/up.png'))))
        self._first_image = first_image
        h.pack_start(widgetutil.align_middle(widgetutil.align_right(
                     self._first_image)))

        # Don't forget to reset the label to blank after we are done
        # fiddling with it.
        first_label.set_text("")
        first_label.set_size_request(width, -1)
        self._first_label = first_label

        h.pack_start(widgetutil.align_middle(widgetutil.align_right(
                     self._first_label, right_pad=20)))

        second_image = widgetutil.HideableWidget(widgetset.ImageDisplay(
                           widgetset.Image(resources.path('images/down.png'))))
        self._second_image = second_image
        # NB: pad the top by 1px - Morgan reckons it looks better when
        # the icon is moved down by 1px.
        h.pack_start(widgetutil.align_middle(widgetutil.align_right(
                     self._second_image), top_pad=1))

        second_label = widgetset.Label("")
        second_label.set_size(widgetconst.SIZE_SMALL)
        second_label.set_size_request(width, -1)
        self._second_label = second_label

        h.pack_start(widgetutil.align_middle(widgetutil.align_right(
                     self._second_label, right_pad=20)))

        v.pack_start(h)
        self.add(v)

        app.frontend_config_watcher.connect('changed', self.on_config_change)

    def on_config_change(self, obj, key, value):
        if ((key == prefs.PRESERVE_X_GB_FREE.key
             or key == prefs.PRESERVE_DISK_SPACE.key)):
            self.update_free_space()

    def update_free_space(self):
        """Updates the free space text on the downloads tab.

        amount -- the total number of bytes free.
        """
        amount = get_available_bytes_for_movies()
        if app.config.get(prefs.PRESERVE_DISK_SPACE):
            available = (app.config.get(prefs.PRESERVE_X_GB_FREE) *
                         1024 * 1024 * 1024)
            available = amount - available

            if available < 0:
                available = available * -1.0
                text = _(
                    "%(available)s below downloads space limit (%(amount)s "
                    "free on disk)",
                    {"amount": displaytext.size_string(amount),
                     "available": displaytext.size_string(available)}
                )
            else:
                text = _(
                    "%(available)s free for downloads (%(amount)s free "
                    "on disk)",
                    {"amount": displaytext.size_string(amount),
                     "available": displaytext.size_string(available)}
                )
        else:
            text = _("%(amount)s free on disk",
                     {"amount": displaytext.size_string(amount)})
        self._free_disk_label.set_text(text)

    def update_rates(self, down_bps, up_bps):
        text_up = text_down = ''
        if up_bps >= 10:
            text_up = _("%(rate)s",
                        {"rate": displaytext.download_rate(up_bps)})
        if down_bps >= 10:
            text_down = _("%(rate)s",
                          {"rate": displaytext.download_rate(down_bps)})

        # first label is always used for upload, while second label is
        # always used for download.  This prevents the text jumping
        # around.
        self._first_label.set_text(text_up)
        self._second_label.set_text(text_down)
        if text_up:
            self._first_image.show()
        else:
            self._first_image.hide()
        if text_down:
            self._second_image.show()
        else:
            self._second_image.hide()

class DownloadTitlebar(ItemListTitlebar):
    """Titlebar with pause/resume/... buttons for downloads, and other
    data.

    :signal pause-all: All downloads should be paused
    :signal resume-all: All downloads should be resumed
    :signal cancel-all: All downloads should be canceled
    :signal settings: The preferences panel downloads tab should be
        opened
    """

    def __init__(self):
        ItemListTitlebar.__init__(self)

        self.create_signal('pause-all')
        self.create_signal('resume-all')
        self.create_signal('cancel-all')
        self.create_signal('settings')

    def _build_titlebar_start(self):
        h = widgetset.HBox(spacing=5)

        pause_button = widgetutil.TitlebarButton(_('Pause All'),
                                                 'download-pause')
        pause_button.connect('clicked', self._on_pause_button_clicked)
        h.pack_start(widgetutil.align_middle(pause_button, top_pad=5,
            bottom_pad=5, left_pad=16))

        resume_button = widgetutil.TitlebarButton(_('Resume All'),
                                                  'download-resume')
        resume_button.connect('clicked', self._on_resume_button_clicked)
        h.pack_start(widgetutil.align_middle(resume_button, top_pad=5,
            bottom_pad=5))

        cancel_button = widgetutil.TitlebarButton(_('Cancel All'),
                                                  'download-cancel')
        cancel_button.connect('clicked', self._on_cancel_button_clicked)
        h.pack_start(widgetutil.align_middle(cancel_button, top_pad=5,
            bottom_pad=5))

        settings_button = widgetutil.TitlebarButton(_('Download Settings'),
                                                    'download-settings')
        settings_button.connect('clicked', self._on_settings_button_clicked)
        h.pack_start(widgetutil.align_middle(settings_button, top_pad=5,
            bottom_pad=5, right_pad=16))
        return h

    def _on_pause_button_clicked(self, widget):
        self.emit('pause-all')

    def _on_resume_button_clicked(self, widget):
        self.emit('resume-all')

    def _on_cancel_button_clicked(self, widget):
        self.emit('cancel-all')

    def _on_settings_button_clicked(self, widget):
        self.emit('settings')

class FeedToolbar(widgetset.Background):
    """Toolbar that appears below the title in a feed.

    :signal remove-feed: (widget) The 'remove feed' button was pressed
    :signal show-settings: (widget) The show settings button was pressed
    :signal auto-download-changed: (widget, value) The auto-download
        setting was changed by the user
    """

    def __init__(self):
        widgetset.Background.__init__(self)
        self.create_signal('remove-feed')
        self.create_signal('show-settings')
        self.create_signal('auto-download-changed')
        hbox = widgetset.HBox(spacing=5)

        settings_button = widgetutil.TitlebarButton(
            _("Settings"), 'feed-settings')
        settings_button.connect('clicked', self._on_settings_clicked)
        self.settings_button = widgetutil.HideableWidget(settings_button)

        autodownload_button = widgetutil.MultiStateTitlebarButton(
            [('autodownload-all', _("Auto-Download All"), "all"),
             ('autodownload-new', _("Auto-Download New"), "new"),
             ('autodownload-off', _("Auto-Download Off"), "off")])
        autodownload_button.connect('clicked', self._on_autodownload_changed)

        self.autodownload_button_actual = autodownload_button
        self.autodownload_button = widgetutil.HideableWidget(
            self.autodownload_button_actual)

        remove_button = widgetutil.TitlebarButton(
            _("Remove podcast"), 'feed-remove-podcast')
        remove_button.connect('clicked', self._on_remove_clicked)
        self.remove_button = remove_button

        hbox.pack_start(widgetutil.align_middle(self.settings_button))
        hbox.pack_start(widgetutil.align_middle(self.autodownload_button))
        hbox.pack_end(widgetutil.align_middle(self.remove_button))
        self.add(widgetutil.pad(hbox, top=4, bottom=4, left=4, right=4))

        self.autodownload_dc = None

    def set_autodownload_mode(self, autodownload_mode):
        if autodownload_mode == 'all':
            self.autodownload_button_actual.set_toggle_state(0)
        elif autodownload_mode == 'new':
            self.autodownload_button_actual.set_toggle_state(1)
        elif autodownload_mode == 'off':
            self.autodownload_button_actual.set_toggle_state(2)

    def draw(self, context, layout):
        key = 74.0 / 255
        top = 223.0 / 255
        bottom = 199.0 / 255

        gradient = widgetset.Gradient(0, 0, 0, context.height)
        gradient.set_start_color((top, top, top))
        gradient.set_end_color((bottom, bottom, bottom))
        context.rectangle(0, 0, context.width, context.height)
        context.gradient_fill(gradient)
        context.set_color((key, key, key))
        context.move_to(0, 0)
        context.rel_line_to(context.width, 0)
        context.stroke()

    def _on_settings_clicked(self, button):
        self.emit('show-settings')

    def _on_remove_clicked(self, button):
        self.emit('remove-feed')

    def _on_autodownload_changed(self, widget):
        if self.autodownload_dc is not None:
            self.autodownload_dc.cancel()
            self.autodownload_dc = None

        toggle_state = self.autodownload_button_actual.get_toggle_state()
        toggle_state = (toggle_state + 1) % 3
        self.autodownload_button_actual.set_toggle_state(toggle_state)
        value = self.autodownload_button_actual.get_toggle_state_information()
        value = value[0]
        self.autodownload_dc = eventloop.add_timeout(
            3, self._on_autodownload_changed_timeout, "autodownload change",
            args=(value,))

    def _on_autodownload_changed_timeout(self, value):
        self.emit('auto-download-changed', value)

class HeaderToolbar(widgetset.Background, SorterWidgetOwner):
    """Toolbar used to sort items and switch views.

    Signals:

    :signal sort-changed: (widget, sort_key, ascending) User changed
        the sort.  sort_key will be one of 'name', 'date', 'size' or
        'length'
    :signal view-all-clicked: User requested to view all items
    :signal toggle-unwatched-clicked: User toggled the
        unwatched/unplayed items only view
    :signal toggle-non-feed-clicked: User toggled the non feed items
        only view
    """
    def __init__(self):
        widgetset.Background.__init__(self)
        SorterWidgetOwner.__init__(self)

        self._button_hbox = widgetset.HBox()
        self._button_hbox_container = widgetutil.HideableWidget(
            self._button_hbox)

        self._hbox = widgetset.HBox()

        self._hbox.pack_end(widgetutil.align_middle(
            self._button_hbox_container, top_pad=1))
        self.pack_hbox_extra()

        self.add(self._hbox)

        self._button_map = {}
        self.sorter_widget_map = self._button_map
        self._make_button(_('Name'), 'name')
        self._make_button(_('Date'), 'date')
        self._make_button(_('Size'), 'size')
        self._make_button(_('Time'), 'length')
        self._button_map['date'].set_sort_order(ascending=False)

        self.filter = WidgetStateStore.get_view_all_filter()

    def pack_hbox_extra(self):
        pass

    def _make_button(self, text, sort_key):
        button = SortBarButton(text)
        button.connect('clicked', self.on_sorter_clicked, sort_key)
        self._button_map[sort_key] = button
        self._button_hbox.pack_start(button)

    def make_filter_switch(self, *args, **kwargs):
        """Helper method to make a SegmentedButtonsRow that switches
        between filters.
        """
        self.filter_switch = segmented.SegmentedButtonsRow(*args, **kwargs)

    def add_filter(self, button_name, signal_name, signal_param, label):
        """Helper method to add a button to the SegmentedButtonsRow
        made in make_filter_switch()

        :param button_name: name of the button
        :param signal_name: signal to emit
        :param label: human readable label for the button
        """

        self.create_signal(signal_name)
        def callback(button):
            self.emit(signal_name, signal_param)
        self.filter_switch.add_text_button(button_name, label, callback)

    def add_filter_switch(self):
        self._hbox.pack_start(widgetutil.align_middle(
            self.filter_switch.make_widget(), left_pad=12))

    def size_request(self, layout):
        width = self._hbox.get_size_request()[0]
        height = self._button_hbox.get_size_request()[1]
        return width, height

    def draw(self, context, layout):
        key = 74.0 / 255
        top = 193.0 / 255
        bottom = 169.0 / 255
        gradient = widgetset.Gradient(0, 0, 0, context.height)
        gradient.set_start_color((top, top, top))
        gradient.set_end_color((bottom, bottom, bottom))
        context.rectangle(0, 0, context.width, context.height)
        context.gradient_fill(gradient)
        context.set_color((key, key, key))
        context.move_to(0, 0)
        context.rel_line_to(context.width, 0)
        context.stroke()

    def toggle_filter(self, filter_):
        # implemented by subclasses
        pass

    def toggle_radio_filter(self, filter_):
        self.filter = filter_
        self._toggle_filter_common()

    def toggle_custom_filter(self, filter_):
        self.filter = WidgetStateStore.toggle_filter(self.filter, filter_)
        self._toggle_filter_common()

    def _toggle_filter_common(self):
        view_all = WidgetStateStore.is_view_all_filter(self.filter)
        unwatched = WidgetStateStore.has_unwatched_filter(self.filter)
        non_feed = WidgetStateStore.has_non_feed_filter(self.filter)
        downloaded = WidgetStateStore.has_downloaded_filter(self.filter)
        self.update_switches(view_all, unwatched, non_feed, downloaded)

class LibraryHeaderToolbar(HeaderToolbar):
    def __init__(self, unwatched_label):
        self.unwatched_label = unwatched_label
        HeaderToolbar.__init__(self)

    def pack_hbox_extra(self):
        self.make_filter_switch(behavior='custom')
        # this "All" is different than other "All"s in the codebase, so it
        # needs to be clarified
        view_all = WidgetStateStore.get_view_all_filter()
        unwatched = WidgetStateStore.get_unwatched_filter()
        non_feed = WidgetStateStore.get_non_feed_filter()
        self.add_filter('view-all', 'toggle-filter', view_all,
                         declarify(_('View|All')))
        self.add_filter('view-unwatched', 'toggle-filter', unwatched,
                        self.unwatched_label)
        self.add_filter('view-non-feed', 'toggle-filter', non_feed,
                        _('Non Podcast'))
        self.add_filter_switch()

    def toggle_filter(self, filter_):
        self.toggle_custom_filter(filter_)

    def update_switches(self, view_all, unwatched, non_feed, downloaded):
        self.filter_switch.set_active('view-all', view_all)
        self.filter_switch.set_active('view-unwatched', unwatched)
        self.filter_switch.set_active('view-non-feed', non_feed)

class SortBarButton(widgetset.CustomButton):
    def __init__(self, text):
        widgetset.CustomButton.__init__(self)
        self._text = text
        self._enabled = False
        self._ascending = False
        self.set_squish_width(True)
        self.set_squish_height(True)

    def get_sort_indicator_visible(self):
        return self._enabled

    def get_sort_order_ascending(self):
        return self._ascending

    def set_sort_indicator_visible(self, visible):
        self._enabled = visible
        self.queue_redraw()

    def set_sort_order(self, ascending):
        self._ascending = ascending
        if self._enabled:
            self.queue_redraw()

    def size_request(self, layout):
        layout.set_font(0.8)
        text_size = layout.textbox(self._text).get_size()
        return text_size[0] + 36, max(text_size[1], 30)

    def draw(self, context, layout):
        # colors are all grayscale
        text = 87.0 / 255
        arrow = 137.0 / 255
        if self._enabled: # selected
            edge = 92.0 / 255
            key = 88.0 / 255
            top = 97.0 / 255
            bottom = 112.0 / 255
            text = 1
            arrow = 1
        elif self.state not in ('hover', 'pressed'):
            edge = 154.0 / 255
            key = 210.0 / 255
            top = 193.0 / 255
            bottom = 169.0 / 255
        else: # hover/pressed
            edge = 123.0 / 255
            key = 149.0 / 255
            top = 145.0 / 255
            bottom = 140.5 / 255

        # key line
        context.move_to(0, 0)
        context.line_to(context.width, 0)
        context.set_color((key, key, key))
        context.stroke()
        # borders
        context.move_to(0.5, 0)
        context.rel_line_to(0, context.height)
        context.move_to(context.width, 0)
        context.rel_line_to(0, context.height)
        context.set_color((edge, edge, edge))
        context.stroke()
        # background
        gradient = widgetset.Gradient(1, 1, 1, context.height - 1)
        gradient.set_start_color((top, top, top))
        gradient.set_end_color((bottom, bottom, bottom))
        context.rectangle(1, 1, context.width - 1, context.height)
        context.gradient_fill(gradient)
        # text
        layout.set_font(0.8)
        layout.set_text_color((text, text, text))
        textbox = layout.textbox(self._text)
        text_size = textbox.get_size()
        y = int((context.height - textbox.get_size()[1]) / 2) - 1.5
        textbox.draw(context, 12, y, text_size[0], text_size[1])
        context.set_color((arrow, arrow, arrow))
        self._draw_triangle(context, text_size[0] + 18)

    def _draw_triangle(self, context, left):
        if self._enabled:
            top = int((context.height - 4) / 2)
            if self._ascending:
                context.move_to(left, top)
                direction = 1
            else:
                context.move_to(left, top + 4)
                direction = -1
            context.rel_line_to(6, 0)
            context.rel_line_to(-3, 4 * direction)
            context.rel_line_to(-3, -4 * direction)
            context.fill()
        else:
            top = int((context.height - 4) / 2)
            context.move_to(left, top)
            context.rel_line_to(6, 0)
            context.rel_line_to(-3, -4)
            context.rel_line_to(-3, 4)
            context.move_to(left, top + 2)
            context.rel_line_to(6, 0)
            context.rel_line_to(-3, 4)
            context.rel_line_to(-3, -4)
            context.fill()

class ItemListBackground(widgetset.Background):
    """Plain white background behind the item lists.
    """

    def draw(self, context, layout):
        if context.style.use_custom_style:
            context.set_color((1, 1, 1))
            context.rectangle(0, 0, context.width, context.height)
            context.fill()

class EmptyListHeader(widgetset.Alignment):
    """Header Label for empty item lists."""
    def __init__(self, text):
        widgetset.Alignment.__init__(self, xalign=0.5, xscale=0.0)
        self.set_padding(24, 0, 0, 0)
        self.label = widgetset.Label(text)
        self.label.set_bold(True)
        self.label.set_color((0.8, 0.8, 0.8))
        self.label.set_size(2)
        self.add(self.label)

class EmptyListDescription(widgetset.Alignment):
    """Label for descriptions of empty item lists."""
    def __init__(self, text):
        widgetset.Alignment.__init__(self, xalign=0.5, xscale=0.5)
        self.set_padding(18)
        self.label = widgetset.Label(text)
        self.label.set_color((0.8, 0.8, 0.8))
        self.label.set_wrap(True)
        self.label.set_size_request(250, -1)
        self.add(self.label)

class ProgressToolbar(widgetset.HBox):
    """Toolbar displayed above ItemViews to show the progress of
    reading new metadata, communicating with a device, and similar
    time-consuming operations.

    Assumes current ETA is accurate; keeps track of its own elapsed
    time.  Displays progress as: elapsed / (elapsed + ETA)

    Rather than have to send a message every time an item is found or
    examined, we cheat a bit: the backend sends signals for batches of
    items (currently 10), and we interpolate the current state based
    on ETA.
    """
    def __init__(self):
        widgetset.HBox.__init__(self)
        loading_icon = widgetset.AnimatedImageDisplay(
                       resources.path('images/load-indicator.gif'))
        self.label = widgetset.Label()
        self.meter = widgetutil.HideableWidget(loading_icon)
        self.label_widget = widgetutil.HideableWidget(self.label)
        self.elapsed = None
        self.eta = None
        self.total = None
        self.remaining = None
        self.mediatype = 'other'
        self.set_up = False

    def setup(self):
        if not self.set_up:
            self.set_up = True
            padding = 380 - self.label.get_width()
            self.pack_start(
                widgetutil.align(
                    self.label_widget, 1, 0.5, 1, 0, 0, 0, padding, 10),
                expand=False)
            self.pack_start(widgetutil.align_left(
                            self.meter, 0, 0, 0, 200), expand=True)
            self.label_widget.show()
            self.meter.show()

    def set_mediatype(self, mediatype):
        self.mediatype = mediatype

    def _update_label(self):
        # TODO: display eta
        state = (self.total-self.remaining, self.total)
        if self.mediatype == 'audio':
            text = _("Importing audio details and artwork: "
                    "{0} of {1}").format(*state)
        elif self.mediatype == 'video':
            text = _("Importing video details and creating thumbnails: "
                    "{0} of {1}").format(*state)
        else:
            text = _("Importing file details: "
                    "{0} of {1}").format(*state)
        self.label.set_text(text)

    def finish(self):
        """Fast-forward through any remaining progress and then
        hide.
        """
        self.update(0, 0.1, self.total)
        # TODO: delay disappearance until bar finishes
        self.label_widget.hide()
        self.meter.hide()

    def update(self, remaining, seconds, total):
        """Correct an existing time estimate. Bar will wait for
        progress to catch up to estimate rather than move backwards.
        """
        self.eta = seconds
        self.total = total
        self.remaining = remaining
        self._update_label()
        self.setup()

class ItemDetailsBackground(widgetset.Background):
    """Nearly white background behind the item details widget
    """

    def draw(self, context, layout):
        if context.style.use_custom_style:
            context.set_color((0.9, 0.9, 0.9))
            context.rectangle(0, 0, context.width, context.height)
            context.fill()

class ItemDetailsExpanderButton(widgetset.CustomButton):
    """Button to expand/contract the item details view"""

    BACKGROUND_GRADIENT_TOP = (0.977,) * 3
    BACKGROUND_GRADIENT_BOTTOM = (0.836,) * 3

    def __init__(self):
        widgetset.CustomButton.__init__(self)
        self.expand_image = imagepool.get_surface(resources.path(
            'images/item-details-expander-arrow.png'))
        self.contract_image = imagepool.get_surface(resources.path(
            'images/item-details-expander-arrow-down.png'))
        self.mode = 'expand'

    def click_should_expand(self):
        return self.mode == 'expand'

    def set_mode(self, mode):
        """Change the mode for the widget.

        possible values "expand" or "contact"
        """
        if mode != 'expand' and mode != 'contract':
            raise ValueError("Unknown mode: %s", mode)
        self.mode = mode
        self.queue_redraw()

    def size_request(self, layout):
        return 30, 13

    def draw(self, context, layout):
        self.draw_gradient(context)
        self.draw_icon(context)

    def draw_gradient(self, context):
        gradient = widgetset.Gradient(0, 0, 0, context.height)
        gradient.set_start_color(self.BACKGROUND_GRADIENT_TOP)
        gradient.set_end_color(self.BACKGROUND_GRADIENT_BOTTOM)
        context.rectangle(0, 0, context.width, context.height)
        context.gradient_fill(gradient)

    def draw_icon(self, context):
        if self.mode == 'expand':
            icon = self.expand_image
        else:
            icon = self.contract_image
        x = int((context.width - icon.width) / 2)
        y = int((context.height - icon.height) / 2)
        icon.draw(context, x, y, icon.width, icon.height)

class ItemDetailsWidget(widgetset.VBox):
    """Widget to display detailed information about an item.

    This usually shows the thumbnail, full description, etc. for the
    selected item.
    """
    PADDING_MIDDLE = 25
    PADDING_RIGHT = 22
    PADDING_ABOVE_TITLE = 25
    PADDING_ABOVE_DESCRIPTION = 8
    PADDING_ABOVE_EXTRA_INFO = 25
    IMAGE_SIZE = (215, 215)
    TEXT_COLOR = (0.176, 0.176, 0.176)
    TITLE_SIZE = 1.1
    EXTRA_INFO_SIZE = 0.85
    # give enough room to display the image, plus some more for the
    # scrollbars
    EXPANDED_HEIGHT = 240

    def __init__(self):
        widgetset.VBox.__init__(self)
        self.allocated_width = -1
        # content_hbox holds our contents
        content_hbox = widgetset.HBox(spacing=self.PADDING_MIDDLE)
        # pack left side
        self.image_widget = widgetset.ImageDisplay()
        content_hbox.pack_start(widgetutil.align_top(self.image_widget))
        # pack right side
        content_hbox.pack_start(widgetutil.pad(self.build_right(),
            right=self.PADDING_RIGHT), expand=True)
        # expander_button is used to expand/collapse our content
        self.expander_button = ItemDetailsExpanderButton()
        self.pack_start(self.expander_button)
        # pack our content
        background = ItemDetailsBackground()
        background.add(widgetutil.align_top(content_hbox))
        self.scroller = widgetset.Scroller(True, True)
        self.scroller.add(background)
        self.scroller.set_size_request(-1, self.EXPANDED_HEIGHT)
        self._expanded = False

    def build_right(self):
        vbox = widgetset.VBox()
        self.title_label = self.build_title()
        self.description_label = self.build_description()
        self.extra_info_label = self.build_extra_info()
        vbox.pack_start(widgetutil.pad(self.title_label,
            top=self.PADDING_ABOVE_TITLE))
        vbox.pack_start(widgetutil.pad(self.description_label,
            top=self.PADDING_ABOVE_DESCRIPTION))
        vbox.pack_start(widgetutil.pad(self.extra_info_label,
            top=self.PADDING_ABOVE_EXTRA_INFO))
        return vbox

    def build_title(self):
        title_label = widgetset.Label()
        title_label.set_selectable(True)
        title_label.set_alignment(widgetconst.TEXT_JUSTIFY_LEFT)
        title_label.set_wrap(True)
        title_label.set_color(self.TEXT_COLOR)
        title_label.set_size(self.TITLE_SIZE)
        title_label.set_bold(True)
        return title_label

    def build_description(self):
        description_label = widgetset.Label()
        description_label.set_selectable(True)
        description_label.set_alignment(widgetconst.TEXT_JUSTIFY_LEFT)
        description_label.set_wrap(True)
        description_label.set_color(self.TEXT_COLOR)
        return description_label

    def build_extra_info(self):
        extra_info_label = widgetset.Label()
        extra_info_label.set_alignment(widgetconst.TEXT_JUSTIFY_LEFT)
        extra_info_label.set_wrap(True)
        extra_info_label.set_color(self.TEXT_COLOR)
        extra_info_label.set_size(self.EXTRA_INFO_SIZE)
        return extra_info_label

    def set_expanded(self, expanded):
        if expanded == self._expanded:
            return
        if expanded:
            self.pack_end(self.scroller)
            self.expander_button.set_mode('contract')
        else:
            self.remove(self.scroller)
            self.expander_button.set_mode('expand')
        self._expanded = expanded

    def set_info(self, info):
        self.title_label.set_text(info.name)
        self.description_label.set_text(info.description_stripped[0])
        self.set_extra_info_text(info)
        image = imagepool.get(info.thumbnail, self.IMAGE_SIZE)
        self.image_widget.set_image(image)
        self.set_label_widths()

    def set_extra_info_text(self, info):
        parts = []
        for attr in (info.display_date, info.display_duration,
                info.display_size, info.file_format):
            if attr:
                parts.append(attr)
        self.extra_info_label.set_text(' | '.join(parts))

    def clear(self):
        self.title_label.set_text('')
        self.description_label.set_text('')
        self.extra_info_label.set_text('')
        self.image_widget.set_image(None)

    def do_size_allocated(self, width, height):
        if width == self.allocated_width:
            return
        self.allocated_width = width
        self.set_label_widths()

    def set_label_widths(self):
        # resize our labels so that they take up exactly all of the width
        if self.image_widget.image is not None:
            image_width = self.image_widget.image.width
        else:
            image_width = 0
        label_width = (self.allocated_width - image_width -
                self.PADDING_MIDDLE - self.PADDING_RIGHT)
        self.title_label.set_size_request(label_width, -1)
        self.description_label.set_size_request(label_width, -1)
        self.extra_info_label.set_size_request(label_width, -1)

class ItemContainerWidget(widgetset.VBox):
    """A Widget for displaying objects that contain items (feeds,
    playlists, folders, downloads tab, etc).

    :attribute titlebar_vbox: VBox for the title bar
    :attribute vbox: VBoxes for standard view and list view
    :attribute list_empty_mode_vbox: VBox for list empty mode
    :attribute toolbar: HeaderToolbar for the widget
    :attribute item_details: ItemDetailsWidget at the bottom of the widget
    """

    def __init__(self, toolbar, view):
        widgetset.VBox.__init__(self)
        self.vbox = {}
        standard_view = WidgetStateStore.get_standard_view_type()
        list_view = WidgetStateStore.get_list_view_type()
        self.vbox[standard_view] = widgetset.VBox()
        self.vbox[list_view] = widgetset.VBox()
        self.titlebar_vbox = widgetset.VBox()
        self.statusbar_vbox = widgetset.VBox()
        self.item_details = ItemDetailsWidget()
        self.list_empty_mode_vbox = widgetset.VBox()
        self.progress_toolbar = ProgressToolbar()
        self.toolbar = toolbar
        if view == standard_view:
            toolbar._button_hbox_container.show()
        self.pack_start(self.titlebar_vbox)
        self.pack_start(self.toolbar)
        self.pack_start(self.progress_toolbar)
        self.background = ItemListBackground()
        self.pack_start(self.background, expand=True)
        self.pack_start(self.item_details)
        self.pack_start(self.statusbar_vbox)
        self.selected_view = view
        self.list_empty_mode = False
        self.background.add(self.vbox[view])

    def toggle_filter(self, filter_):
        self.toolbar.toggle_filter(filter_)

    def switch_to_view(self, view, toolbar=None):
        if self.selected_view != view:
            if not self.list_empty_mode:
                self.background.remove()
                self.background.add(self.vbox[view])
            self.selected_view = view
            if WidgetStateStore.is_standard_view(view):
                self.toolbar._button_hbox_container.show()
            else:
                self.toolbar._button_hbox_container.hide()

    def set_list_empty_mode(self, enabled):
        if enabled != self.list_empty_mode:
            self.background.remove()
            if enabled:
                self.background.add(self.list_empty_mode_vbox)
            else:
                self.background.add(self.vbox[self.selected_view])
            self.list_empty_mode = enabled

    def get_progress_meter(self):
        """Return a ProgressToolbar attached to the display."""
        return self.progress_toolbar
