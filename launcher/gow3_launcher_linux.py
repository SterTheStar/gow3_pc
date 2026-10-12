#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""God of War III (gow3) launcher for Linux (GTK4 / libadwaita).

The counterpart of launcher/gow3_launcher_win.py: picks the game folder, edits gow3.ini
(upscaler, output, effects) and the start-up options passed to run.sh as environment
variables (frame rate, presentation, HDR, ...), starts and stops the game and shows its
output. Launcher settings live in ~/.config/gow3-launcher/settings.json.

Run directly with system PyGObject/GTK4/libadwaita installed, else through
`launcher/gow3-launcher.sh` (nix-shell fallback). `--play` starts the game with the
saved settings and no window (Steam Deck game mode, AppImage).
"""

import json
import os
import re
import signal
import sys
from pathlib import Path

import gow3_config as cfg

sys.path.insert(0, str(cfg.PORT_DIR / 'scripts'))
import save_backup  # noqa: E402
from mods import apply_preset, conflicts, discover as discover_mods  # noqa: E402
from patches import EXCLUSIVE_PREFIX, external_patches, game_patchable  # noqa: E402

import gi  # noqa: E402

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

sys.path.insert(0, str(cfg.PORT_DIR / 'launcher'))
import gow3_lang  # noqa: E402
from gow3_assets import fsr411_problem  # noqa: E402

LANG = 'en'
MAX_LOG_LINES = 5000


def _(en, ru=None):
    if LANG == 'ru':
        return ru or en
    return gow3_lang.table(LANG).get(en) or en


def flat_button(icon, tooltip, handler):
    button = Gtk.Button(icon_name=icon, valign=Gtk.Align.CENTER, tooltip_text=tooltip)
    button.add_css_class('flat')
    button.connect('clicked', handler)
    return button


def open_folder(window, path):
    """Opens `path` in the file manager (created first, so a new saves folder opens)."""
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    Gtk.FileLauncher.new(Gio.File.new_for_path(str(path))).launch(window, None, None)


def combo_row(title, subtitle, choices, current):
    model = Gtk.StringList.new([_(en, ru) for _value, (en, *rest) in choices
                                for ru in [rest[0] if rest else None]])
    row = Adw.ComboRow(title=title, model=model)
    if subtitle:
        row.set_subtitle(subtitle)
    values = [value for value, _text in choices]
    row.set_selected(values.index(current) if current in values else 0)
    row.values = values
    return row


def combo_value(row):
    return row.values[row.get_selected()]


class FolderList:
    """A preferences group with a folder row and one switch row per item below it."""

    def __init__(self, group):
        self.group = group
        self.rows = []

    def clear(self):
        for _, row in self.rows:
            self.group.remove(row)
        self.rows = []

    def add(self, key, row):
        self.group.add(row)
        self.rows.append((key, row))


class LauncherWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=f'{cfg.APP_NAME} (PC)')
        self.set_default_size(800, 860)
        self.settings = cfg.load_settings()
        global LANG
        LANG = self.settings.get('ui_language') or cfg.system_language()
        self.ini, self.ini_lines = cfg.load_ini()
        self.process = None
        self.stream = None
        self.mod_order = []
        self.build()
        self.connect('close-request', self.on_close)
        if self.settings.get('check_updates', False):
            Gio.Task.run_in_thread(lambda *_: self.check_update())

    # ---- layout ---------------------------------------------------------------------

    def build(self):
        """(Re)creates the window's content in the current launcher language."""
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)
        self.launch_button = Gtk.Button()
        self.launch_button.connect('clicked', self.on_launch)
        header.pack_end(self.launch_button)
        toolbar.add_top_bar(header)

        self.toasts = Adw.ToastOverlay()
        self.toasts.set_child(self.stack)
        toolbar.set_content(self.toasts)
        self.set_content(toolbar)

        log_text = self.log_view.get_buffer().get_text(
            *self.log_view.get_buffer().get_bounds(), False) if hasattr(self, 'log_view') else ''
        self.stack.add_titled_with_icon(self.build_home_page(), 'home', _('Home'), 'go-home-symbolic')
        self.stack.add_titled_with_icon(self.build_display_page(), 'display', _('Display'),
                                        'video-display-symbolic')
        self.stack.add_titled_with_icon(self.build_performance_page(), 'performance', _('Performance'),
                                        'utilities-system-monitor-symbolic')
        self.stack.add_titled_with_icon(self.build_game_page(), 'game', _('Game'), 'applications-games-symbolic')
        self.stack.add_titled_with_icon(self.build_patches_page(), 'patches', _('Patches'),
                                        'document-edit-symbolic')
        self.stack.add_titled_with_icon(self.build_mods_page(), 'mods', _('Mods'), 'package-x-generic-symbolic')
        self.stack.add_titled_with_icon(self.build_controls_page(), 'controls', _('Controls'),
                                        'input-gaming-symbolic')
        self.stack.add_titled_with_icon(self.build_advanced_page(), 'advanced', _('Advanced'),
                                        'preferences-system-symbolic')
        self.stack.add_titled_with_icon(self.build_log_page(), 'log', _('Log'),
                                        'utilities-terminal-symbolic')
        self.log_view.get_buffer().set_text(log_text)
        self.update_launch_button()
        self.update_game_status()
        self.refresh_status_label()

    def toast(self, message):
        self.toasts.add_toast(Adw.Toast(title=message))

    def update_launch_button(self):
        running = self.process is not None
        self.launch_button.set_label(_('Stop') if running else _('PLAY'))
        self.launch_button.remove_css_class('destructive-action' if not running else 'suggested-action')
        self.launch_button.add_css_class('destructive-action' if running else 'suggested-action')

    # ---- home -----------------------------------------------------------------------

    def build_home_page(self):
        page = Adw.PreferencesPage()
        game = Adw.PreferencesGroup(title=_('Game'))
        self.game_row = Adw.ActionRow(title=_('Game folder'))
        self.game_status = Gtk.Image()
        self.game_row.add_suffix(self.game_status)
        self.game_row.add_suffix(flat_button('folder-open-symbolic',
                                             _('Choose the folder with eboot.bin'), self.on_choose_game))
        self.game_row.add_suffix(flat_button('system-file-manager-symbolic', _('Open'),
                                             lambda _b: open_folder(self, cfg.game_dir_of(self.settings))))
        game.add(self.game_row)
        self.status_row = Adw.ActionRow(title=_('Ready check'))
        game.add(self.status_row)
        page.add(game)

        quick = Adw.PreferencesGroup(title=_('Quick settings'))
        self.quick_display = combo_row(_('Display mode'), None, cfg.DISPLAY_MODES,
                                       self.ini.get('display_mode', 'windowed'))
        quick.add(self.quick_display)
        self.quick_fps = combo_row(_('Frame rate limit'), None, cfg.FPS_LIMITS,
                                   self.ini.get('fps_limit', '0'))
        quick.add(self.quick_fps)
        self.quick_resolution = combo_row(_('Render resolution'),
                                          _('The size the game draws at; higher is sharper and slower.'),
                                          cfg.RESOLUTION_CHOICES, self.ini.get('render_resolution', 'native'))
        quick.add(self.quick_resolution)
        self.quick_engine = combo_row(_('Engine frame rate'),
                                      _('The highest frame rate the game itself runs at (a game patch).'),
                                      cfg.ENGINE_FPS_CHOICES, self.ini.get('engine_fps', '120'))
        quick.add(self.quick_engine)
        page.add(quick)

        hint = Adw.PreferencesGroup()
        hint.add(Adw.ActionRow(title=_("In the game: Insert or R3+L2\nopens the port's menu."),
                               subtitle=_('Cheats, image, performance and the performance overlay.')))
        page.add(hint)
        return page

    def update_game_status(self):
        path = cfg.game_dir_of(self.settings)
        info = cfg.core.game_info(str(path))
        ok = info is not None
        self.game_row.set_subtitle(
            f'{path} · {info[0]} · {info[1]}' if ok else _('Game folder not set'))
        self.game_status.set_from_icon_name('object-select-symbolic' if ok else 'dialog-warning-symbolic')
        self.launch_button.set_sensitive(ok or self.process is not None)

    def refresh_status_label(self):
        path = cfg.game_dir_of(self.settings)
        if not (path / 'eboot.bin').is_file():
            self.status_row.set_subtitle(_('Choose the game folder first.'))
            return
        resolution = dict((v, t[0]) for v, t in cfg.RESOLUTION_CHOICES).get(
            self.ini.get('render_resolution', 'native'), '?')
        if not game_patchable(str(path)):
            self.status_row.set_subtitle(_('{} · 60 FPS engine · patches not available for this game build').format(
                resolution))
            return
        title_id, _profile = cfg.core.game_profile_of(str(path))
        names = self.settings.get('game_patches', {}).get(title_id) or []
        self.status_row.set_subtitle(_('{} · {} FPS engine · {} patches').format(
            resolution, self.ini.get('engine_fps', '120'), len(names)))

    # ---- display --------------------------------------------------------------------

    def build_display_page(self):
        page = Adw.PreferencesPage()
        window = Adw.PreferencesGroup(title=_('Window'))
        window.add(self.quick_display)
        self.vsync_row = Adw.SwitchRow(title='VSync',
                                       subtitle=_('Off allows tearing for the lowest latency.'),
                                       active=self.ini.get('vsync', '1') == '1')
        window.add(self.vsync_row)
        self.hdr_row = Adw.SwitchRow(title=_('Allow HDR output'),
                                     subtitle=_('When HDR is on in the system and the display supports it.'),
                                     active=bool(self.settings.get('hdr', False)))
        window.add(self.hdr_row)
        page.add(window)

        scaling = Adw.PreferencesGroup(title=_('Scaling and sharpening'))
        self.fsr1_row = Adw.SwitchRow(
            title=_('Upscale with FSR 1'),
            subtitle=_('Enlarges a smaller game image to the window. With Render resolution 720p '
                       '(Performance) it raises the FPS; at the size of the window it does nothing.'),
            active=self.ini.get('fsr1', '0') == '1')
        scaling.add(self.fsr1_row)
        self.rcas_row = Adw.SwitchRow(title=_('Sharpening (RCAS)'), active=self.ini.get('rcas', '1') == '1')
        scaling.add(self.rcas_row)
        self.rcas_strength = Adw.SpinRow.new_with_range(0, 100, 1)
        self.rcas_strength.set_title(_('Sharpening strength'))
        try:
            self.rcas_strength.set_value(float(self.ini.get('rcas_strength', '75')))
        except ValueError:
            self.rcas_strength.set_value(75)
        scaling.add(self.rcas_strength)
        page.add(scaling)

        overlay = Adw.PreferencesGroup(title=_('Overlay'))
        self.show_fps_row = Adw.SwitchRow(
            title=_('Show performance overlay'),
            subtitle=_('Items, size and corner: in-game menu (Insert / R3+L2) > Overlay.'),
            active=self.ini.get('show_fps', '1') == '1')
        overlay.add(self.show_fps_row)
        page.add(overlay)
        return page

    # ---- performance ------------------------------------------------------------------

    def build_performance_page(self):
        page = Adw.PreferencesPage()
        rate = Adw.PreferencesGroup(title=_('Frame rate'))
        rate.add(self.quick_fps)
        self.frames_queued = combo_row(
            _('Frames ahead of the GPU'),
            _('1 keeps frame pacing even; more can raise FPS when the graphics card is the limit.'),
            cfg.FRAMES_QUEUED, self.ini.get('frames_queued', '1'))
        rate.add(self.frames_queued)
        rate.add(self.quick_engine)
        rate.add(self.quick_resolution)
        page.add(rate)

        shaders = Adw.PreferencesGroup(title=_('Shaders'))
        self.pipeline_cache_row = Adw.SwitchRow(
            title=_('Use pipeline binary cache (A/B test)'),
            subtitle=_('Reuse saved Vulkan pipelines. Disable to compare FPS with normal compilation. '
                       'Takes effect on the next launch; saved caches are preserved.'),
            active=self.settings.get('pipeline_binary_cache', True))
        shaders.add(self.pipeline_cache_row)
        self.background_warmup_row = Adw.SwitchRow(
            title=_('Load shader cache in the background'),
            subtitle=_('The game opens at once and the saved shaders load while you play; a small note in '
                       'the corner shows the progress. Switch it off to wait on the loading screen instead.'),
            active=self.settings.get('background_warmup', True))
        shaders.add(self.background_warmup_row)
        self.async_row = Adw.SwitchRow(
            title=_('Compile new shaders in the background'),
            subtitle=_('Fewer stutters in new areas: the game keeps running while new shaders compile. An '
                       'object or effect may appear a moment later. Switch it off if something stays missing.'),
            active=self.ini.get('async_shaders', '1') == '1')
        shaders.add(self.async_row)
        self.parallel_row = Adw.SwitchRow(
            title=_('Fast shader cache loading (parallel)'),
            subtitle=_('Loading screen only: builds the cached pipelines on several CPU threads.'),
            active=self.settings.get('parallel_warmup', True))
        shaders.add(self.parallel_row)
        page.add(shaders)

        data = Adw.PreferencesGroup(title=_('GPU data'))
        self.stream_row = combo_row(
            _('Stream buffer memory'),
            _('System RAM can improve FPS on NVIDIA laptops. Choose GPU memory to restore the previous '
              'behavior. Applies on the next game launch.'), cfg.STREAM_MEMORY,
            self.settings.get('stream_memory', 'cached'))
        data.add(self.stream_row)
        self.stale_row = Adw.SwitchRow(
            title=_('Game reads GPU data without waiting (big FPS gain)'),
            subtitle=_('About 40 to 66 FPS in a heavy scene. Needs deferred GPU readbacks. Switch it off if '
                       'lighting or objects look wrong.'),
            active=self.ini.get('stale_readback', '1') == '1')
        data.add(self.stale_row)
        self.deferred_row = Adw.SwitchRow(
            title=_('Deferred GPU readbacks'),
            subtitle=_('Fewer waits for the GPU. Switch it off if textures or shadows look wrong.'),
            active=self.ini.get('deferred_readback', '1') == '1')
        data.add(self.deferred_row)
        self.draw_pipe_row = combo_row(
            _('Two-stage GPU pipeline'), _('20–30% faster; switch it off if the game is unstable.'),
            cfg.DRAW_PIPE, self.settings.get('draw_pipe', ''))
        data.add(self.draw_pipe_row)
        self.readbacks_row = combo_row(
            _('GPU readbacks'), _('How exactly data the GPU writes is copied back for the game.'),
            cfg.READBACKS, self.settings.get('readbacks', ''))
        data.add(self.readbacks_row)
        page.add(data)
        return page

    # ---- game -------------------------------------------------------------------------

    def build_game_page(self):
        page = Adw.PreferencesPage()
        cheats = Adw.PreferencesGroup(title=_('Cheats'))
        self.double_jumps_row = Adw.SwitchRow(
            title=_('Infinite Double Jumps'),
            subtitle=_('Repeat air jumps without landing. Also available in the in-game Cheats menu.'),
            active=self.ini.get('cheat_double_jumps', '0') == '1')
        cheats.add(self.double_jumps_row)
        self.very_hard_row = Adw.SwitchRow(
            title=_('Unlock Very Hard'),
            subtitle=_('Select Very Hard without completing the game. Does not change existing save '
                       'difficulty. In-game: enable before opening New Game.'),
            active=self.ini.get('cheat_very_hard', '0') == '1')
        cheats.add(self.very_hard_row)
        page.add(cheats)

        folders = Adw.PreferencesGroup(title=_('Folders'))
        folders.add(self.game_row)
        self.user_row = Adw.ActionRow(title=_('Saves folder'))
        self.user_status = Gtk.Image()
        self.user_row.add_suffix(self.user_status)
        self.user_row.add_suffix(flat_button('folder-open-symbolic', _('Choose the saves folder'),
                                             self.on_choose_user))
        self.user_row.add_suffix(flat_button('system-file-manager-symbolic', _('Open'),
                                             lambda _b: open_folder(self, cfg.user_dir_of(self.settings))))
        self.user_reset = flat_button('edit-undo-symbolic', _('Use the default folder'), self.on_reset_user)
        self.user_row.add_suffix(self.user_reset)
        folders.add(self.user_row)
        self.update_user_status()
        page.add(folders)

        saves = Adw.PreferencesGroup(
            title=_('Save backups'),
            description=_('Made before every start, newest first; the last {} are kept. Restoring backs up '
                          'the current saves first.').format(save_backup.KEEP))
        self.backup_row = Adw.ComboRow(title=_('Backup'))
        saves.add(self.backup_row)
        buttons = Adw.ActionRow()
        buttons.add_suffix(flat_button('view-refresh-symbolic', _('Refresh'), lambda _b: self.refresh_backups()))
        buttons.add_suffix(flat_button('edit-undo-symbolic', _('Restore'), lambda _b: self.restore_backup()))
        saves.add(buttons)
        page.add(saves)
        self.refresh_backups()

        language = Adw.PreferencesGroup(title=_('Language'))
        self.language_row = combo_row(_('Game language'), None, cfg.LANGUAGES,
                                      self.settings.get('language', '1'))
        language.add(self.language_row)
        self.player_row = Adw.EntryRow(title=_('Player name'),
                                       subtitle=_('Where the game shows the PSN name; empty: the default.'),
                                       text=str(self.settings.get('player_name', '')))
        language.add(self.player_row)
        page.add(language)
        return page

    def update_user_status(self):
        path = cfg.user_dir_of(self.settings)
        custom = bool(self.settings.get('user_dir'))
        self.user_row.set_subtitle(str(path))
        saves = path / 'savedata'
        found = saves.is_dir() and any(saves.iterdir())
        self.user_status.set_from_icon_name('object-select-symbolic' if found else 'document-new-symbolic')
        self.user_status.set_tooltip_text(
            _('✓ Saves found in {}').format(path) if found
            else _('• No saves yet: the game creates them in {}').format(path))
        self.user_reset.set_sensitive(custom)

    def choose_folder(self, title, current, done):
        dialog = Gtk.FileDialog(title=title)
        if Path(current).is_dir():
            dialog.set_initial_folder(Gio.File.new_for_path(str(current)))

        def finish(dialog, result):
            try:
                folder = dialog.select_folder_finish(result)
            except GLib.Error:
                return
            if folder:
                done(folder.get_path())
        dialog.select_folder(self, None, finish)

    def on_choose_game(self, _button):
        def chosen(path):
            self.settings['game_dir'] = path
            self.update_game_status()
            self.refresh_status_label()
            self.refresh_game_patches()
            self.store()
        self.choose_folder(_('Choose the folder with eboot.bin'), cfg.game_dir_of(self.settings), chosen)

    def on_choose_user(self, _button):
        def chosen(path):
            self.settings['user_dir'] = path
            self.update_user_status()
            self.refresh_backups()
            self.store()
        self.choose_folder(_('Choose the saves folder'), cfg.user_dir_of(self.settings), chosen)

    def on_reset_user(self, _button):
        self.settings['user_dir'] = ''
        self.update_user_status()
        self.refresh_backups()
        self.store()

    def refresh_backups(self):
        names = save_backup.list_backups(cfg.user_dir_of(self.settings))
        self.backup_row.set_model(Gtk.StringList.new(names))
        self.backup_row.set_selected(0 if names else -1)
        self.backup_row.set_sensitive(bool(names))

    def restore_backup(self):
        if self.backup_row.get_selected() < 0:
            return
        if self.process:
            self.toast(_('Close the game before restoring saves.'))
            return
        name = self.backup_row.get_selected_item().get_string()
        try:
            safety = save_backup.restore(cfg.user_dir_of(self.settings), name)
        except (OSError, ValueError) as error:
            self.toast(_('Could not restore: {}').format(error))
            return
        self.refresh_backups()
        self.update_user_status()
        message = _('Saves restored from {}.').format(name)
        if safety:
            message += ' ' + _(' Previous saves: {}.').format(safety.name)
        self.toast(message)

    # ---- patches ------------------------------------------------------------------------

    def build_patches_page(self):
        page = Adw.PreferencesPage()
        self.game_patches_group = Adw.PreferencesGroup(
            title=_('Game patches'),
            description=_('Community patches of the selected game, applied at start.'))
        self.game_patch_list = FolderList(self.game_patches_group)
        page.add(self.game_patches_group)
        self.refresh_game_patches()

        third = Adw.PreferencesGroup(
            title=_('Third-party patches'),
            description=_('shadPS4/GoldHEN XML patch files for this game version.'))
        self.patches_folder_row = Adw.ActionRow(title=_('Patches folder'))
        self.patches_folder_row.add_suffix(flat_button('folder-open-symbolic', _('Choose the patches folder'),
                                                       self.on_choose_patches))
        self.patches_folder_row.add_suffix(flat_button(
            'system-file-manager-symbolic', _('Open'),
            lambda _b: open_folder(self, self.patches_dir())))
        self.patches_folder_row.add_suffix(flat_button('view-refresh-symbolic', _('Refresh'),
                                                       lambda _b: self.refresh_patches()))
        third.add(self.patches_folder_row)
        self.patch_list = FolderList(third)
        page.add(third)
        self.refresh_patches()
        return page

    def patches_dir(self):
        return Path(self.settings.get('patches_dir') or cfg.DATA_DIR / 'patches').expanduser()

    def refresh_game_patches(self):
        import xml.etree.ElementTree as ET
        if not hasattr(self, 'game_patch_list'):
            return
        self.store_game_patches()
        self.game_patch_list.clear()
        folder = str(cfg.game_dir_of(self.settings))
        title_id, profile = cfg.core.game_profile_of(folder)
        if not profile:
            self.game_patches_group.set_description(_('No built-in patches for this game.'))
            return
        _ids, xml, version = profile[:3]
        metas = [m for m in ET.parse(xml).getroot().iter('Metadata') if m.get('AppVer') == version]
        chosen = self.settings.get('game_patches', {}).get(title_id)
        on = set(chosen) if chosen is not None else {m.get('Name') for m in metas
                                                     if m.get('isEnabled', 'false').lower() == 'true'}
        self.game_patches_group.set_description(
            _('{} · version {} · {}. Resolution and frame rate patches follow Performance.').format(
                title_id, version, xml.name))
        self.game_patch_title = title_id
        for meta in metas:
            name = meta.get('Name')
            if name.startswith(EXCLUSIVE_PREFIX) or name.startswith('Frame Rate Patch'):
                continue
            row = Adw.SwitchRow(title=GLib.markup_escape_text(name), active=name in on)
            if meta.get('Author'):
                row.set_subtitle(GLib.markup_escape_text(_('Author: {}').format(meta.get('Author'))))
            if meta.get('Note'):
                row.set_tooltip_text(meta.get('Note').replace('\\n', '\n'))
            self.game_patch_list.add(name, row)

    def store_game_patches(self):
        """The page's selection into settings (without resolution/frame rate: run.sh derives
        those from gow3.ini through derive_graphics_env)."""
        if not getattr(self, 'game_patch_title', None):
            return
        self.settings.setdefault('game_patches', {})[self.game_patch_title] = [
            name for name, row in self.game_patch_list.rows if row.get_active()]

    def save_patch_profile(self):
        """patches.json: the switches that differ from each file's isEnabled."""
        enabled, disabled = [], []
        for key, row in self.patch_list.rows:
            if row.get_active() != row.default:
                (enabled if row.get_active() else disabled).append(key)
        path = cfg.DATA_DIR / 'patches.json'
        try:
            profile = json.loads(path.read_text())
        except (OSError, ValueError):
            profile = {}
        shown = {key for key, _ in self.patch_list.rows}
        profile = {'enabled': sorted({k for k in profile.get('enabled', []) if k not in shown} | set(enabled)),
                   'disabled': sorted({k for k in profile.get('disabled', []) if k not in shown} | set(disabled))}
        cfg.DATA_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(profile, indent=2, ensure_ascii=False) + '\n')

    def refresh_patches(self):
        if not hasattr(self, 'patch_list'):
            return
        self.save_patch_profile()
        self.patch_list.clear()
        directory = self.patches_dir()
        try:
            profile = json.loads((cfg.DATA_DIR / 'patches.json').read_text())
        except (OSError, ValueError):
            profile = {}
        _title_id, game = cfg.core.game_profile_of(str(cfg.game_dir_of(self.settings)))
        found = external_patches(directory, game[2], game[1], game[0]) if game else []
        for key, _path, meta in found:
            default = meta.get('isEnabled', 'false').lower() == 'true'
            active = key in profile.get('enabled', []) or (default and key not in profile.get('disabled', []))
            subtitle = key.split('/', 1)[0]
            if meta.get('Author'):
                subtitle += ' · ' + _('Author: {}').format(meta.get('Author'))
            row = Adw.SwitchRow(title=GLib.markup_escape_text(meta.get('Name') or key),
                                subtitle=GLib.markup_escape_text(subtitle), active=active)
            if meta.get('Note'):
                row.set_tooltip_text(meta.get('Note').replace('\\n', '\n'))
            row.default = default
            self.patch_list.add(key, row)
        self.patches_folder_row.set_subtitle(str(directory) if found else f'{directory} — {_("No patch files")}')

    def on_choose_patches(self, _button):
        self.save_patch_profile()

        def chosen(path):
            self.settings['patches_dir'] = path
            self.refresh_patches()
            self.store()
        self.choose_folder(_('Choose the patches folder'), self.patches_dir(), chosen)

    # ---- mods ---------------------------------------------------------------------------

    def build_mods_page(self):
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(
            title=_('Mods'),
            description=_('Files layered over the game at start; the game files are never changed.'))
        self.mods_enabled_row = Adw.SwitchRow(title=_('Load mods'),
                                              active=self.settings.get('mods_enabled', True))
        group.add(self.mods_enabled_row)
        self.mods_folder_row = Adw.ActionRow(title=_('Mods folder'))
        self.mods_folder_row.add_suffix(flat_button('folder-open-symbolic', _('Choose the mods folder'),
                                                    self.on_choose_mods))
        self.mods_folder_row.add_suffix(flat_button('system-file-manager-symbolic', _('Open'),
                                                    lambda _b: open_folder(self, self.mods_dir())))
        self.mods_folder_row.add_suffix(flat_button('view-refresh-symbolic', _('Refresh'),
                                                    lambda _b: self.on_refresh_mods()))
        group.add(self.mods_folder_row)
        self.mod_list = FolderList(group)
        page.add(group)
        self.refresh_mods()

        presets = Adw.PreferencesGroup(title=_('Preset'))
        self.preset_row = Adw.ComboRow(title=_('Preset'))
        presets.add(self.preset_row)
        buttons = Adw.ActionRow()
        buttons.add_suffix(flat_button('document-open-symbolic', _('Load'), lambda _b: self.load_mod_preset()))
        buttons.add_suffix(flat_button('document-save-symbolic', _('Save'), lambda _b: self.save_mod_preset()))
        buttons.add_suffix(flat_button('edit-delete-symbolic', _('Delete'), lambda _b: self.delete_mod_preset()))
        presets.add(buttons)
        page.add(presets)
        self.refresh_presets()

        self.conflict_row = Adw.ActionRow(title=_('Conflicts'))
        found_group = Adw.PreferencesGroup()
        found_group.add(self.conflict_row)
        page.add(found_group)
        return page

    def mods_dir(self):
        return Path(self.settings.get('mods_dir') or cfg.DATA_DIR / 'mods').expanduser()

    def save_mod_profile(self):
        cfg.DATA_DIR.mkdir(parents=True, exist_ok=True)
        rows = self.mod_list.rows
        profile = {'order': [name for name, _ in rows],
                   'disabled': [name for name, row in rows if not row.get_active()]}
        (cfg.DATA_DIR / 'mods.json').write_text(json.dumps(profile, indent=2, ensure_ascii=False) + '\n')

    def refresh_mods(self):
        if not hasattr(self, 'mod_list'):
            return
        self.mod_list.clear()
        self.mods_folder_row.set_subtitle(str(self.mods_dir()))
        try:
            profile = json.loads((cfg.DATA_DIR / 'mods.json').read_text())
        except (OSError, ValueError):
            profile = {}
        available = discover_mods(self.mods_dir())
        if set(self.mod_order) != set(available):
            known = [n for n in profile.get('order', []) if n in available]
            self.mod_order = known + [n for n in available if n not in known]
        for name in self.mod_order:
            row = Adw.SwitchRow(title=name, active=name not in profile.get('disabled', []))
            row.add_suffix(flat_button('go-up-symbolic', _('Load earlier'),
                                       lambda _b, n=name: self.move_mod(n, -1)))
            row.add_suffix(flat_button('go-down-symbolic', _('Load later'),
                                       lambda _b, n=name: self.move_mod(n, 1)))
            self.mod_list.add(name, row)
        self.refresh_conflicts()

    def refresh_conflicts(self):
        enabled = [name for name, row in self.mod_list.rows if row.get_active()]
        found = conflicts(self.mods_dir(), enabled)
        lines = [_('⚠ {} file(s) replaced by more than one mod (the last one wins):').format(len(found))] if found else []
        lines += [f'   {path}: {" > ".join(owners)}' for path, owners in found[:12]]
        if len(found) > 12:
            lines.append(_('   … and {} more').format(len(found) - 12))
        self.conflict_row.set_subtitle('\n'.join(lines) if lines else _('No conflicts.'))

    def move_mod(self, name, direction):
        rows = self.mod_list.rows
        index = next(i for i, (n, _) in enumerate(rows) if n == name)
        target = index + direction
        if 0 <= target < len(rows):
            rows[index], rows[target] = rows[target], rows[index]
            self.save_mod_profile()
            self.refresh_mods()

    def on_refresh_mods(self):
        self.save_mod_profile()
        self.refresh_mods()

    def on_choose_mods(self, _button):
        self.save_mod_profile()

        def chosen(path):
            self.settings['mods_dir'] = path
            self.refresh_mods()
            self.store()
        self.choose_folder(_('Choose the mods folder'), self.mods_dir(), chosen)

    def load_presets(self):
        try:
            return json.loads(cfg.MOD_PRESETS.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}

    def refresh_presets(self, keep=''):
        presets = self.load_presets()
        self.preset_row.set_model(Gtk.StringList.new(sorted(presets)))
        names = sorted(presets)
        self.preset_row.set_selected(names.index(keep) if keep in names else -1)

    def save_mod_preset(self):
        name = self.preset_row.get_selected_item()
        name = name.get_string().strip() if name else ''
        if not name:
            self.toast(_('Type a name for the preset first.'))
            return
        presets = self.load_presets()
        presets[name] = {'order': [n for n, _ in self.mod_list.rows],
                         'disabled': [n for n, row in self.mod_list.rows if not row.get_active()]}
        cfg.MOD_PRESETS.write_text(json.dumps(presets, indent=2), encoding='utf-8')
        self.refresh_presets(name)

    def load_mod_preset(self):
        if self.preset_row.get_selected() < 0:
            return
        name = self.preset_row.get_selected_item().get_string()
        preset = self.load_presets().get(name)
        if not isinstance(preset, dict):
            return
        order, disabled = apply_preset(preset, [n for n, _ in self.mod_list.rows])
        self.mod_order = order
        try:
            profile = json.loads((cfg.DATA_DIR / 'mods.json').read_text())
        except (OSError, ValueError):
            profile = {}
        profile.update({'order': order, 'disabled': disabled})
        cfg.DATA_DIR.mkdir(parents=True, exist_ok=True)
        (cfg.DATA_DIR / 'mods.json').write_text(json.dumps(profile, indent=2) + '\n')
        self.refresh_mods()
        self.refresh_presets(name)

    def delete_mod_preset(self):
        if self.preset_row.get_selected() < 0:
            return
        name = self.preset_row.get_selected_item().get_string()
        presets = self.load_presets()
        if presets.pop(name, None) is not None:
            cfg.MOD_PRESETS.write_text(json.dumps(presets, indent=2), encoding='utf-8')
        self.refresh_presets()

    # ---- controls -------------------------------------------------------------------------

    def build_controls_page(self):
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(
            title=_('Controls'),
            description=_('Which key or gamepad button presses each button of the game.'))
        style = self.settings.get('pad_style')
        if style not in cfg.PAD_STYLES:
            style = 'playstation'
        self.pad_style_row = combo_row(_('Gamepad names'), None,
                                       [('playstation', ('PlayStation',)), ('xbox', ('Xbox',))], style)
        self.pad_style_row.connect('notify::selected', lambda *_: self.relabel_pad_controls())
        group.add(self.pad_style_row)
        saved = self.settings.get('controls') if isinstance(self.settings.get('controls'), dict) else {}
        self.key_rows, self.pad_rows = {}, {}
        keys = Adw.PreferencesGroup(title=_('Keyboard'))
        pads = Adw.PreferencesGroup(title=_('Gamepad'))
        for button, key, pad in cfg.CONTROLS:
            key_row = Adw.EntryRow(title=cfg.PS_BUTTON_NAMES[button],
                                   text=saved.get('key', {}).get(button) or key)
            keys.add(key_row)
            self.key_rows[button] = key_row
            if pad:
                labels = list(cfg.PAD_STYLES[style].values())
                current = saved.get('pad', {}).get(button) or pad
                label = cfg.PAD_STYLES[style].get(current, current)
                pad_row = combo_row(cfg.PS_BUTTON_NAMES[button], None,
                                    [(name, (text,)) for name, text in cfg.PAD_STYLES[style].items()],
                                    current)
                pad_row.set_selected(labels.index(label) if label in labels else 0)
                pads.add(pad_row)
                self.pad_rows[button] = pad_row
        page.add(group)
        page.add(keys)
        page.add(pads)
        note = Adw.PreferencesGroup()
        note.add(Adw.ActionRow(
            title=_('Keys use SDL names: letters, digits, Space, Return, Tab, Left Shift, Left Ctrl, Left Alt, '
                    'Backspace, Up, Down, Left, Right, F1 to F12. A name the game does not know keeps the default. '
                    'L2/R2 on the gamepad stay on the triggers. Insert and R3+L2 always open the port\'s menu. '
                    'Applied when the game starts.')))
        page.add(note)
        reset = Adw.PreferencesGroup()
        reset_row = Adw.ActionRow(title=_('Reset to defaults'))
        reset_row.add_suffix(flat_button('edit-undo-symbolic', _('Reset to defaults'),
                                        lambda _b: self.reset_controls()))
        reset.add(reset_row)
        page.add(reset)
        return page

    def pad_style(self):
        return combo_value(self.pad_style_row)

    def relabel_pad_controls(self):
        labels = cfg.PAD_STYLES[self.pad_style()]
        for button, row in self.pad_rows.items():
            name = self.pad_sdl_name(button)
            row.set_model(Gtk.StringList.new(list(labels.values())))
            row.set_selected(list(labels).index(name) if name in labels else 0)

    def pad_sdl_name(self, button):
        labels = cfg.PAD_STYLES[self.pad_style()]
        current = self.pad_rows[button].get_selected_item()
        current = current.get_string() if current else ''
        return next((n for n, label in labels.items() if label == current), current)

    def reset_controls(self):
        for button, key, pad in cfg.CONTROLS:
            self.key_rows[button].set_text(key)
            if pad and button in self.pad_rows:
                labels = cfg.PAD_STYLES[self.pad_style()]
                self.pad_rows[button].set_selected(
                    list(labels).index(pad) if pad in labels else 0)

    # ---- advanced --------------------------------------------------------------------------

    def build_advanced_page(self):
        page = Adw.PreferencesPage()
        launcher = Adw.PreferencesGroup(title=_('Launcher'))
        self.ui_language_row = combo_row(_('Launcher language'),
                                         _('Applies when the launcher opens again.'),
                                         cfg.core.LANGUAGE_NAMES,
                                         self.settings.get('ui_language', ''))
        self.ui_language_row.connect('notify::selected', self.on_ui_language)
        launcher.add(self.ui_language_row)
        self.close_row = Adw.SwitchRow(title=_('Close the launcher when the game starts'),
                                       active=bool(self.settings.get('close_on_play', False)))
        launcher.add(self.close_row)
        self.check_updates_row = Adw.SwitchRow(title=_('Check for updates when the launcher opens'),
                                               active=bool(self.settings.get('check_updates', False)))
        launcher.add(self.check_updates_row)
        update = Adw.ActionRow(title=_('Version {}').format(cfg.VERSION))
        update.add_suffix(flat_button('view-refresh-symbolic', _('Check for updates'),
                                      lambda _b: Gio.Task.run_in_thread(lambda *_: self.check_update(manual=True))))
        launcher.add(update)
        shortcut = Adw.ActionRow(title=_('Desktop shortcut'))
        shortcut.add_suffix(flat_button('document-new-symbolic', _('Create the desktop shortcut'),
                                        lambda _b: self.install_desktop_file()))
        launcher.add(shortcut)
        page.add(launcher)

        files = Adw.PreferencesGroup(title=_('Files'))
        for title, path in ((_('Port folder'), cfg.DATA_DIR), ('gow3.ini', cfg.ini_path())):
            row = Adw.ActionRow(title=title, subtitle=str(path))
            row.add_suffix(flat_button('system-file-manager-symbolic', _('Open'),
                                       lambda _b, p=path: open_folder(self, p)))
            files.add(row)
        cache = Adw.ActionRow(
            title=_('Clear shader cache'),
            subtitle=_('If the game only shows a black screen, this usually helps.'))
        cache.add_suffix(flat_button('edit-delete-symbolic', _('Clear shader cache'),
                                     lambda _b: self.clear_cache()))
        files.add(cache)
        page.add(files)

        dev = Adw.PreferencesGroup(title=_('Diagnostics'))
        self.frame_stats_row = Adw.SwitchRow(title=_('Frame statistics in the log (every 5 s)'),
                                             active=bool(self.settings.get('frame_stats', False)))
        dev.add(self.frame_stats_row)
        self.gpu_profile_row = Adw.SwitchRow(title=_('GPU time per pass in the log'),
                                             active=bool(self.settings.get('gpu_profile', False)))
        dev.add(self.gpu_profile_row)
        self.perf_diag_row = Adw.SwitchRow(
            title=_('GPU wait and readback report in the log (every 5 s)'),
            subtitle=_('Shows where the frame waits for the GPU and which reads cause it. For performance tests.'),
            active=bool(self.settings.get('perf_diag', False)))
        dev.add(self.perf_diag_row)
        self.capture_row = Adw.SwitchRow(
            title=_('F11 captures a frame for analysis'),
            subtitle=_('Records how the next frame is drawn into the captures folder.'),
            active=bool(self.settings.get('frame_capture_key', False)))
        dev.add(self.capture_row)
        self.validation_row = Adw.SwitchRow(
            title=_('Vulkan validation layers (needs the Vulkan SDK; much slower)'),
            active=bool(self.settings.get('vk_validation', False)))
        dev.add(self.validation_row)
        self.extra_row = Adw.EntryRow(title=_('Extra variables'),
                                      subtitle=_('NAME=value pairs separated by spaces (README lists them).'),
                                      text=str(self.settings.get('extra_env', '')))
        dev.add(self.extra_row)
        page.add(dev)
        return page

    def on_ui_language(self, row, _param):
        choice = combo_value(row)
        if choice == self.settings.get('ui_language', ''):
            return
        self.store()
        self.settings['ui_language'] = choice
        cfg.save_settings(self.settings)
        global LANG
        LANG = choice or cfg.system_language()
        if self.process:
            self.toast(_('Applies when the launcher opens again.'))
            return
        # The row that emitted the signal is replaced right after.
        GLib.idle_add(lambda: self.build() or False)

    def clear_cache(self):
        if self.process:
            return
        import shutil
        shutil.rmtree(cfg.user_dir_of(self.settings) / 'cache', ignore_errors=True)
        self.toast(_('Shader cache cleared. The next start stutters for a few minutes while it is rebuilt.'))

    def install_desktop_file(self):
        """Installs the .desktop shortcut for this launcher (source tree or package)."""
        source = cfg.PORT_DIR / 'launcher' / 'io.github.gow3pc.Launcher.desktop'
        target = Path.home() / '.local/share/applications' / 'io.github.gow3pc.Launcher.desktop'
        try:
            text = source.read_text(encoding='utf-8')
        except OSError as error:
            self.toast(str(error))
            return
        if not cfg.PACKAGED:
            text = text.replace('Exec=gow3', f'Exec={cfg.PORT_DIR / "launcher/gow3-launcher.sh"}')
            text = text.replace('Icon=gow3', f'Icon={cfg.PORT_DIR / "launcher/gow3.png"}')
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding='utf-8')
        except OSError as error:
            self.toast(str(error))
            return
        self.toast(_('Shortcut created on the desktop.'))

    # ---- updates ---------------------------------------------------------------------------

    def check_update(self, manual=False):
        try:
            version, _url, page = latest_appimage_release()
        except (OSError, ValueError, KeyError) as failure:
            if manual:
                GLib.idle_add(lambda: self.toast(_('Could not check for updates: {}').format(failure)) or False)
            return

        def show():
            if cfg.core.version_tuple(version) > cfg.core.version_tuple(cfg.VERSION):
                self.toast(_('Version {} is available.').format(version))
                Gtk.UriLauncher.new(page).launch(self, None, None)
            elif manual:
                self.toast(_('You have the latest version ({}).').format(cfg.VERSION))
            return False
        GLib.idle_add(show)

    # ---- log page ----------------------------------------------------------------------------

    def build_log_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.log_view = Gtk.TextView(editable=False, monospace=True, cursor_visible=False,
                                     wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.log_view.set_top_margin(8)
        self.log_view.set_left_margin(8)
        self.log_view.set_right_margin(8)
        scroller = Gtk.ScrolledWindow(vexpand=True, child=self.log_view)
        self.log_scroller = scroller
        box.append(scroller)
        return box

    def append_log(self, text):
        buffer = self.log_view.get_buffer()
        buffer.insert(buffer.get_end_iter(), text)
        extra = buffer.get_line_count() - MAX_LOG_LINES
        if extra > 0:
            buffer.delete(buffer.get_start_iter(), buffer.get_iter_at_line(extra)[1])
        adj = self.log_scroller.get_vadjustment()
        GLib.idle_add(lambda: adj.set_value(adj.get_upper()) or False)

    # ---- state -----------------------------------------------------------------------------------

    def store(self):
        s = self.settings
        self.store_game_patches()
        s['game_dir'] = str(cfg.game_dir_of(s))
        s['language'] = combo_value(self.language_row)
        s['player_name'] = self.player_row.get_text().strip()
        s['hdr'] = self.hdr_row.get_active()
        s['draw_pipe'] = combo_value(self.draw_pipe_row)
        s['readbacks'] = combo_value(self.readbacks_row)
        s['stream_memory'] = combo_value(self.stream_row)
        s['background_warmup'] = self.background_warmup_row.get_active()
        s['parallel_warmup'] = self.parallel_row.get_active()
        s['pipeline_binary_cache'] = self.pipeline_cache_row.get_active()
        s['perf_diag'] = self.perf_diag_row.get_active()
        s['frame_stats'] = self.frame_stats_row.get_active()
        s['gpu_profile'] = self.gpu_profile_row.get_active()
        s['vk_validation'] = self.validation_row.get_active()
        s['frame_capture_key'] = self.capture_row.get_active()
        s['extra_env'] = self.extra_row.get_text().strip()
        s['close_on_play'] = self.close_row.get_active()
        s['check_updates'] = self.check_updates_row.get_active()
        s['pad_style'] = self.pad_style()
        s['controls'] = {
            'key': {b: self.key_rows[b].get_text().strip() for b, _k, _p in cfg.CONTROLS
                    if self.key_rows[b].get_text().strip()},
            'pad': {b: self.pad_sdl_name(b) for b in self.pad_rows},
        }
        # Keep only real changes (same rule as the Windows launcher).
        s['controls'] = {kind: cfg.core.control_changes(
            {b: (s['controls'][kind].get(b) or d or '') for b, k, p in cfg.CONTROLS
             for d in [(k if kind == 'key' else p)]}, kind) for kind in ('key', 'pad')}
        cfg.save_settings(s)
        ini = dict(self.ini)
        ini.update({
            'display_mode': combo_value(self.quick_display),
            'fps_limit': combo_value(self.quick_fps),
            'render_resolution': combo_value(self.quick_resolution),
            'engine_fps': combo_value(self.quick_engine),
            'vsync': '1' if self.vsync_row.get_active() else '0',
            'fsr1': '1' if self.fsr1_row.get_active() else '0',
            'rcas': '1' if self.rcas_row.get_active() else '0',
            'rcas_strength': str(int(self.rcas_strength.get_value())),
            'show_fps': '1' if self.show_fps_row.get_active() else '0',
            'frames_queued': combo_value(self.frames_queued),
            'pipeline_binary_cache': ini.get('pipeline_binary_cache', '1'),
            'async_shaders': '1' if self.async_row.get_active() else '0',
            'deferred_readback': '1' if self.deferred_row.get_active() else '0',
            'stale_readback': '1' if self.stale_row.get_active() else '0',
            'cheat_double_jumps': '1' if self.double_jumps_row.get_active() else '0',
            'cheat_very_hard': '1' if self.very_hard_row.get_active() else '0',
        })
        cfg.save_ini({key: ini[key] for key in cfg.INI_DEFAULTS if key in ini}, self.ini_lines)
        self.ini, self.ini_lines = cfg.load_ini()
        self.save_mod_profile()
        self.save_patch_profile()
        self.refresh_status_label()

    def environment(self):
        return cfg.game_environment(self.settings, self.ini)

    # ---- process -----------------------------------------------------------------------------------

    def on_launch(self, _button):
        if self.process:
            self.stop_game()
            return
        self.store()
        game = cfg.game_dir_of(self.settings)
        if not (game / 'eboot.bin').is_file():
            self.toast(_('Choose the game folder with eboot.bin (CUSA01623).'))
            self.stack.set_visible_child_name('game')
            return
        user_dir = cfg.user_dir_of(self.settings)
        user_dir.mkdir(parents=True, exist_ok=True)
        try:
            if made := save_backup.backup(user_dir):
                print(f'Saves backed up to {made}', flush=True)
        except OSError as error:
            print(f'Save backup failed, starting anyway: {error}', flush=True)
        # The window icon for the game (SDL loads BMP without extra libraries).
        out = cfg.DATA_DIR / 'out'
        out.mkdir(parents=True, exist_ok=True)
        if icon := cfg.window_icon(out):
            os.environ['GOW3_WINDOW_ICON'] = str(icon)
        self.log_view.get_buffer().set_text('')
        launcher = Gio.SubprocessLauncher.new(
            Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE)
        launcher.set_environ([f'{k}={v}' for k, v in self.environment().items()])
        launcher.set_cwd(str(cfg.PORT_DIR))
        try:
            # setsid: the game and its helpers form one process group, stopped together.
            self.process = launcher.spawnv(['setsid', 'bash', str(cfg.PORT_DIR / 'run.sh')])
        except GLib.Error as error:
            self.toast(_('Could not start: {}').format(error.message))
            return
        self.stream = Gio.DataInputStream.new(self.process.get_stdout_pipe())
        self.read_line()
        self.process.wait_async(None, self.on_exit)
        self.update_launch_button()
        self.stack.set_visible_child_name('log')
        if self.settings.get('close_on_play'):
            self.close()

    def read_line(self):
        self.stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self.on_line)

    def on_line(self, stream, result):
        try:
            line, _length = stream.read_line_finish_utf8(result)
        except GLib.Error:
            return
        if line is None:
            return
        if 'restarting through run.sh' in line:
            self.toast(_('Restarting with the new settings…'))
        self.append_log(line + '\n')
        self.read_line()

    def stop_game(self):
        if not self.process:
            return
        pid = int(self.process.get_identifier())
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            self.process.force_exit()
        GLib.timeout_add_seconds(3, self.kill_if_running, pid)

    def kill_if_running(self, pid):
        if self.process:
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
        return False

    def on_exit(self, process, result):
        try:
            process.wait_finish(result)
        except GLib.Error:
            pass
        status = process.get_exit_status() if process.get_if_exited() else -1
        self.process = None
        self.update_launch_button()
        self.update_game_status()
        self.refresh_status_label()
        self.append_log(_('\nThe game exited (code {}).\n').format(status))

    def on_close(self, _window):
        try:
            self.store()
        except Exception:  # never keep the window open over a settings problem
            pass
        if self.process:
            self.stop_game()
        return False


class LauncherApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id='io.github.gow3pc.Launcher',
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        window = self.get_active_window() or LauncherWindow(self)
        window.present()


def latest_appimage_release():
    """(version, AppImage URL, page URL) of the newest GitHub release."""
    import urllib.request
    request = urllib.request.Request(cfg.core.RELEASES_API, headers={'Accept': 'application/vnd.github+json',
                                                                     'User-Agent': 'gow3-launcher'})
    with urllib.request.urlopen(request, timeout=15) as response:
        release = json.load(response)
    version = '.'.join(re.findall(r'\d+', release['tag_name']))
    url = next((asset['browser_download_url'] for asset in release.get('assets', [])
                if asset.get('name', '').lower().endswith('.appimage')), None)
    return version, url, release.get('html_url') or cfg.core.RELEASES_PAGE


def play():
    """--play: the game with the saved settings, no window (Steam Deck game mode)."""
    settings = cfg.load_settings()
    ini, _lines = cfg.load_ini()
    game = cfg.game_dir_of(settings)
    if not (game / 'eboot.bin').is_file():
        print('gow3: choose the game folder in the launcher first', file=sys.stderr)
        return 1
    user_dir = cfg.user_dir_of(settings)
    user_dir.mkdir(parents=True, exist_ok=True)
    try:
        save_backup.backup(user_dir)
    except OSError as error:
        print(f'Save backup failed, starting anyway: {error}', flush=True)
    os.chdir(cfg.PORT_DIR)
    os.execvpe('bash', ['bash', str(cfg.PORT_DIR / 'run.sh')], cfg.game_environment(settings, ini))


if __name__ == '__main__':
    if '--play' in sys.argv[1:]:
        sys.exit(play())
    sys.exit(LauncherApp().run(sys.argv))
