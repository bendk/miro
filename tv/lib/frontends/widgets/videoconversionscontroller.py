# Miro - an RSS based video player application
# Copyright (C) 2005-2010 Participatory Culture Foundation
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

from miro.plat import resources
from miro.gtcache import gettext as _
from miro.frontends.widgets import style
from miro.frontends.widgets import cellpack
from miro.frontends.widgets import imagepool
from miro.frontends.widgets import separator
from miro.frontends.widgets import widgetconst
from miro.frontends.widgets import widgetutil
from miro.frontends.widgets import itemlistwidgets
from miro.plat.frontends.widgets import widgetset

from miro.videoconversion import conversion_manager

class VideoConversionsController(object):

    def __init__(self):
        self.widget = widgetset.VBox()
        self.build_widget()

    def build_widget(self):
        image_path = resources.path("images/icon-conversions_large.png")
        icon = imagepool.get(image_path)
        titlebar = VideoConversionsTitleBar(_("Conversions"), icon)
        self.widget.pack_start(titlebar)

        sep = separator.HSeparator((0.85, 0.85, 0.85), (0.95, 0.95, 0.95))
        self.widget.pack_start(sep)

        self.stop_all_button = widgetset.Button(_('Stop All Conversions'), style='smooth')
        self.stop_all_button.set_size(widgetconst.SIZE_SMALL)
        self.stop_all_button.set_color(widgetset.TOOLBAR_GRAY)
        self.stop_all_button.disable()
        self.stop_all_button.connect('clicked', self.on_cancel_all)

        reveal_button = widgetset.Button(_('Show Conversion Folder'), style='smooth')
        reveal_button.set_size(widgetconst.SIZE_SMALL)
        reveal_button.set_color(widgetset.TOOLBAR_GRAY)
        reveal_button.connect('clicked', self.on_reveal)

        toolbar = itemlistwidgets.DisplayToolbar()
        hbox = widgetset.HBox()
        hbox.pack_start(widgetutil.pad(self.stop_all_button, top=8, bottom=8, left=8))
        hbox.pack_end(widgetutil.pad(reveal_button, top=8, bottom=8, right=8))
        toolbar.add(hbox)
        self.widget.pack_start(toolbar)
        
        self.iter_map = dict()
        self.model = widgetset.TableModel('object')
        self.table = VideoConversionTableView(self.model)
        self.table.connect_weak('hotspot-clicked', self.on_hotspot_clicked)
        scroller = widgetset.Scroller(False, True)
        scroller.add(self.table)

        self.widget.pack_start(scroller, expand=True)

        conversion_manager.fetch_tasks_list()
    
    def on_cancel_all(self, obj):
        conversion_manager.cancel_all()

    def on_reveal(self, obj):
        conversion_manager.reveal_conversions_folder()
        
    def on_hotspot_clicked(self, table_view, name, itr):
        task = table_view.model[itr][0]
        if name == 'cancel' and not task.is_running():
            conversion_manager.cancel_pending(task)
        elif name == 'interrupt' and task.is_running():
            conversion_manager.cancel_running(task)

    def handle_task_list(self, running_tasks, pending_tasks):
        for task in running_tasks:
            self.iter_map[task.key] = self.model.append(task)
        for task in pending_tasks:
            self.iter_map[task.key] = self.model.append(task)
        self.table.model_changed()
        self._update_buttons_state()

    def handle_task_added(self, task):
        self.iter_map[task.key] = self.model.append(task)
        self.table.model_changed()
        self._update_buttons_state()
    
    def handle_all_tasks_canceled(self):
        for key in self.iter_map.keys():
            itr = self.iter_map.pop(key)
            self.model.remove(itr)
        self.table.model_changed()
        self._update_buttons_state()
    
    def handle_task_canceled(self, task):
        self.handle_task_completed(task)
        self._update_buttons_state()
    
    def handle_task_progress(self, task):
        if task.key in self.iter_map:
            itr = self.iter_map[task.key]
            self.model.update_value(itr, 0, task)
            self.table.model_changed()
    
    def handle_task_completed(self, task):
        if task.key in self.iter_map:
            itr = self.iter_map.pop(task.key)
            self.model.remove(itr)
            self.table.model_changed()
            self._update_buttons_state()
    
    def _update_buttons_state(self):
        if len(self.iter_map) > 0:
            self.stop_all_button.enable()
        else:
            self.stop_all_button.disable()


class VideoConversionsTitleBar(itemlistwidgets.ItemListTitlebar):
    def _build_titlebar_extra(self):
        pass


class VideoConversionTableView(widgetset.TableView):
    def __init__(self, model):
        widgetset.TableView.__init__(self, model)
        self.set_show_headers(False)

        self.renderer = VideoConversionCellRenderer()        
        self.column = widgetset.TableColumn('conversion', self.renderer, data=0)
        self.column.set_min_width(600)
        self.add_column(self.column)

        self.set_draws_selection(False)
        self.set_show_headers(False)
        self.allow_multiple_select(False)
        self.set_auto_resizes(True)
        self.set_background_color(widgetutil.WHITE)


class VideoConversionCellRenderer(widgetset.CustomCellRenderer):
    THUMB_WIDTH = 70
    THUMB_HEIGHT = 48
    SEPARATOR_COLOR = (0.5, 0.5, 0.5)
    RUNNING_TASK_TEXT_COLOR = (0.4, 0.4, 0.4)
    PENDING_TASK_TEXT_COLOR = (0.8, 0.8, 0.8)
    INTERRUPT_BUTTON = imagepool.get_surface(resources.path('images/video-download-cancel.png'))
    THUMB_OVERLAY = imagepool.get_surface(resources.path('images/thumb-overlay.png'), (THUMB_WIDTH, THUMB_HEIGHT))

    def get_size(self, style, layout):
        return 600, self.THUMB_HEIGHT + 13

    def render(self, context, layout, selected, hotspot, hover):
        self.hotspot = hotspot
        packing = self._pack_all(layout)
        packing.render_layout(context)

    def hotspot_test(self, style, layout, x, y, width, height):
        self.hotspot = None
        packing = self._pack_all(layout)
        hotspot_info = packing.find_hotspot(x, y, width, height)
        if hotspot_info is None:
            return None
        hotspot, x, y, width, height = hotspot_info
        return hotspot

    def _pack_all(self, layout):
        hbox = cellpack.HBox(spacing=8)
        hbox.pack(self._pack_thumbnail(layout))
        hbox.pack(self._pack_info(layout), expand=True)
        return cellpack.pad(hbox, left=8, right=8)

    def _pack_thumbnail(self, layout):
        thumb = cellpack.DrawingArea(self.THUMB_WIDTH, self.THUMB_HEIGHT, self._draw_thumbnail)
        return cellpack.align_middle(cellpack.align_center(thumb))

    def _pack_info(self, layout):
        vbox = cellpack.VBox()
        if self.data.is_running():
            layout.set_text_color(self.RUNNING_TASK_TEXT_COLOR)
        else:
            layout.set_text_color(self.PENDING_TASK_TEXT_COLOR)
        layout.set_font(1.1, bold=True)
        title = cellpack.ClippedTextLine(layout.textbox(self.data.item_info.name))
        vbox.pack(cellpack.pad(title, top=10))
        if self.data.is_running():
            vbox.pack_end(self._pack_progress(layout), expand=True)
        else:
            vbox.pack_end(self._pack_pending_controls(layout), expand=False)
        return vbox

    def _pack_progress(self, layout):
        hbox = cellpack.HBox()
        hbox.pack(cellpack.align_middle(cellpack.align_center(self._progress_textbox(layout))), expand=True)
        hbox.pack(cellpack.pad(cellpack.align_right(cellpack.Hotspot('interrupt', self.INTERRUPT_BUTTON)), right=3))
        background = cellpack.Background(cellpack.align_middle(hbox), min_width=356, min_height=20)
        background.set_callback(style.ProgressBarDrawer(self.data.progress).draw)
        return cellpack.pad(background, top=13, bottom=10)

    def _pack_pending_controls(self, layout):
        hbox = cellpack.HBox()
        layout.set_font(1.0)
        layout.set_text_color(self.PENDING_TASK_TEXT_COLOR)
        hbox.pack(cellpack.pad(layout.textbox("(Pending)"), right=8))
        layout.set_font(0.8)
        cancel_button = layout.button("Cancel", self.hotspot=='cancel', style='webby')
        hbox.pack(cellpack.Hotspot('cancel', cancel_button))
        return cellpack.pad(hbox, bottom=10)

    def _progress_textbox(self, layout):
        layout.set_font(0.80, bold=True)
        layout.set_text_color((1.0, 1.0, 1.0))
        progress = int(self.data.progress * 100)
        return layout.textbox('%d%%' % (progress,))

    def _draw_thumbnail(self, context, x, y, width, height):
        fraction = 1.0
        if not self.data.is_running():
            fraction = 0.4
        icon = imagepool.get_surface(self.data.item_info.thumbnail, (width, height))
        widgetutil.draw_rounded_icon(context, icon, x, y, width, height, fraction=fraction)
        self.THUMB_OVERLAY.draw(context, x, y, width, height, fraction=fraction)
