import sys
import os
import re
import zipfile
import shutil
import requests
import json
import time
import ctypes
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QSlider, QCheckBox, QLineEdit, QFileDialog,
    QProgressBar, QTextEdit, QGroupBox, QSizePolicy, QDialog,
    QListWidget, QListWidgetItem, QMessageBox
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QIcon, QFont, QColor

SETTINGS_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
ICON_FILE        = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
TRACKED_FILE     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "installed_fonts.json")
WINDOWS_FONT_DIR = os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)
adapter = requests.adapters.HTTPAdapter(
    pool_connections=20, pool_maxsize=20,
    max_retries=requests.adapters.Retry(total=3, backoff_factor=0.3)
)
SESSION.mount("https://", adapter)
SESSION.mount("http://",  adapter)

# ─────────────────────────────────────────────────────────────────────────────
# admin elevation  – relaunch self as admin if not already
# ─────────────────────────────────────────────────────────────────────────────

def is_admin() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False


def relaunch_as_admin():
    """Re-run this very script with admin rights and exit current process."""
    script = os.path.abspath(sys.argv[0])
    params = " ".join(f'"{a}"' for a in sys.argv[1:])
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, f'"{script}" {params}', None, 1
    )
    sys.exit(0)

# ─────────────────────────────────────────────────────────────────────────────
# settings
# ─────────────────────────────────────────────────────────────────────────────

def load_settings() -> dict:
    defaults = {
        "save_dir":      "C:/fonts/dafonts/",
        "max_gb":        10,
        "install_fonts": False,
        "delete_after":  False,
    }
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                defaults.update(json.load(f))
        except Exception:
            pass
    return defaults


def save_settings(s: dict):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(s, f, indent=2)

# ─────────────────────────────────────────────────────────────────────────────
# font tracking
# ─────────────────────────────────────────────────────────────────────────────

def load_tracked() -> list:
    if os.path.exists(TRACKED_FILE):
        try:
            with open(TRACKED_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_tracked(lst: list):
    with open(TRACKED_FILE, "w") as f:
        json.dump(lst, f, indent=2)


def track_font(font_name: str):
    lst = load_tracked()
    if font_name not in lst:
        lst.append(font_name)
    save_tracked(lst)


def untrack_font(font_name: str):
    lst = load_tracked()
    if font_name in lst:
        lst.remove(font_name)
    save_tracked(lst)

# ─────────────────────────────────────────────────────────────────────────────
# font install  – multiple methods, each falling back to the next
# ─────────────────────────────────────────────────────────────────────────────

def _copy_font_file(src: str, dest: str) -> bool:
    """Try every copy method available."""

    # ── method 1: plain shutil (works when already admin) ────────────────
    try:
        shutil.copy2(src, dest)
        return True
    except PermissionError:
        pass
    except Exception:
        pass

    # ── method 2: Windows shell SHFileOperation via ctypes ────────────────
    try:
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCT(ctypes.Structure):
            _fields_ = [
                ("hwnd",                  wintypes.HWND),
                ("wFunc",                 wintypes.UINT),
                ("pFrom",                 wintypes.LPCWSTR),
                ("pTo",                   wintypes.LPCWSTR),
                ("fFlags",                ctypes.c_int),
                ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings",         ctypes.c_void_p),
                ("lpszProgressTitle",     wintypes.LPCWSTR),
            ]

        FO_COPY   = 0x0002
        FOF_FLAGS = 0x0014  # FOF_NOCONFIRMATION | FOF_SILENT

        src_buf  = src  + "\x00\x00"
        dest_buf = dest + "\x00\x00"

        op = SHFILEOPSTRUCT()
        op.hwnd   = None
        op.wFunc  = FO_COPY
        op.pFrom  = src_buf
        op.pTo    = dest_buf
        op.fFlags = FOF_FLAGS

        ret = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        if ret == 0:
            return True
    except Exception:
        pass

    # ── method 3: xcopy via subprocess (hidden window) ────────────────────
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags    |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        result = subprocess.run(
            ["xcopy", "/Y", "/Q", src, os.path.dirname(dest) + "\\"],
            startupinfo=si,
            capture_output=True,
        )
        if result.returncode == 0:
            return True
    except Exception:
        pass

    # ── method 4: powershell Copy-Item (runs in user context but can
    #              sometimes bypass locks that python can't) ──────────────
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags    |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        cmd = (
            f'Copy-Item -Path "{src}" '
            f'-Destination "{dest}" -Force'
        )
        result = subprocess.run(
            ["powershell", "-NonInteractive", "-Command", cmd],
            startupinfo=si,
            capture_output=True,
        )
        if result.returncode == 0:
            return True
    except Exception:
        pass

    return False


def install_font(font_path: str, log_fn=None) -> bool:
    """Install font into Windows Fonts folder using any method that works."""
    font_name = os.path.basename(font_path)
    dest      = os.path.join(WINDOWS_FONT_DIR, font_name)

    # skip if identical file is already there
    if os.path.exists(dest):
        try:
            if os.path.getsize(dest) == os.path.getsize(font_path):
                track_font(font_name)
                return True
        except Exception:
            pass

    ok = _copy_font_file(font_path, dest)
    if not ok:
        if log_fn:
            log_fn(f"  install failed (all methods exhausted): {font_name}")
        return False

    # register with GDI so it is usable without reboot
    try:
        ctypes.windll.gdi32.AddFontResourceExW(dest, 0x10, 0)
        # broadcast WM_FONTCHANGE so running apps see the new font
        HWND_BROADCAST = 0xFFFF
        WM_FONTCHANGE  = 0x001D
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_FONTCHANGE, 0, 0, 0x0002, 1000, None
        )
    except Exception:
        pass

    # registry entry so font persists across reboots
    try:
        import winreg
        ext      = os.path.splitext(font_name)[1].lower()
        reg_name = (os.path.splitext(font_name)[0] +
                    (" (TrueType)" if ext == ".ttf" else " (OpenType)"))
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts",
            0, winreg.KEY_SET_VALUE
        )
        winreg.SetValueEx(key, reg_name, 0, winreg.REG_SZ, font_name)
        winreg.CloseKey(key)
    except Exception as e:
        if log_fn:
            log_fn(f"  registry warning ({font_name}): {e}")

    track_font(font_name)
    if log_fn:
        log_fn(f"  installed: {font_name}")
    return True


def uninstall_font(font_name: str, log_fn=None) -> bool:
    dest = os.path.join(WINDOWS_FONT_DIR, font_name)
    try:
        ctypes.windll.gdi32.RemoveFontResourceExW(dest, 0x10, 0)
        HWND_BROADCAST = 0xFFFF
        WM_FONTCHANGE  = 0x001D
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_FONTCHANGE, 0, 0, 0x0002, 1000, None
        )
    except Exception:
        pass

    try:
        import winreg
        ext      = os.path.splitext(font_name)[1].lower()
        reg_name = (os.path.splitext(font_name)[0] +
                    (" (TrueType)" if ext == ".ttf" else " (OpenType)"))
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts",
            0, winreg.KEY_SET_VALUE
        )
        winreg.DeleteValue(key, reg_name)
        winreg.CloseKey(key)
    except Exception:
        pass

    try:
        if os.path.exists(dest):
            os.remove(dest)
    except Exception as e:
        if log_fn:
            log_fn(f"  remove error ({font_name}): {e}")
        return False

    untrack_font(font_name)
    if log_fn:
        log_fn(f"  removed: {font_name}")
    return True

# ─────────────────────────────────────────────────────────────────────────────
# extraction helper
# ─────────────────────────────────────────────────────────────────────────────

def extract_and_clean(archive_path: str, extract_dir: str,
                      install: bool, delete_after: bool,
                      log_fn=None) -> list:
    kept = []
    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(extract_dir)
        os.remove(archive_path)

        for root, dirs, files in os.walk(extract_dir):
            for file in files:
                fp = os.path.join(root, file)
                if file.lower().endswith((".ttf", ".otf")):
                    dest = os.path.join(extract_dir, file)
                    if fp != dest:
                        shutil.move(fp, dest)
                        fp = dest
                    kept.append(fp)
                else:
                    try:
                        os.remove(fp)
                    except Exception:
                        pass

        for root, dirs, files in os.walk(extract_dir, topdown=False):
            for d in dirs:
                try:
                    os.rmdir(os.path.join(root, d))
                except OSError:
                    pass

        for fp in kept:
            if install:
                install_font(fp, log_fn)
            if delete_after and install:
                try:
                    os.remove(fp)
                except Exception:
                    pass

    except zipfile.BadZipFile:
        if log_fn:
            log_fn(f"  bad zip: {os.path.basename(archive_path)}")
        try:
            os.remove(archive_path)
        except Exception:
            pass
    except Exception as e:
        if log_fn:
            log_fn(f"  extract error: {e}")
    return kept

# ─────────────────────────────────────────────────────────────────────────────
# remove-fonts window
# ─────────────────────────────────────────────────────────────────────────────

class RemoveFontsWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("remove dafont fonts")
        if os.path.exists(ICON_FILE):
            self.setWindowIcon(QIcon(ICON_FILE))
        self.setMinimumSize(540, 500)
        self._build()
        self._populate()

    def _build(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(12, 12, 12, 12)

        f = QFont()
        f.setPointSize(9)

        info = QLabel(
            "below are fonts installed by dafont downloader.\n"
            "select the ones you want to remove, then click 'remove selected'."
        )
        info.setFont(f)
        info.setWordWrap(True)
        root.addWidget(info)

        sel_row = QHBoxLayout()
        btn_all  = QPushButton("select all")
        btn_none = QPushButton("deselect all")
        btn_all.setFont(f)
        btn_none.setFont(f)
        btn_all.clicked.connect(self._select_all)
        btn_none.clicked.connect(self._deselect_all)
        sel_row.addWidget(btn_all)
        sel_row.addWidget(btn_none)
        sel_row.addStretch()
        root.addLayout(sel_row)

        self.lst = QListWidget()
        self.lst.setFont(QFont("Consolas", 8))
        self.lst.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        root.addWidget(self.lst)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setFixedHeight(90)
        self.log.setFont(QFont("Consolas", 8))
        root.addWidget(self.log)

        act_row = QHBoxLayout()
        self.btn_remove = QPushButton("remove selected")
        btn_close       = QPushButton("close")
        self.btn_remove.setFont(f)
        btn_close.setFont(f)
        self.btn_remove.setFixedHeight(32)
        btn_close.setFixedHeight(32)
        self.btn_remove.clicked.connect(self._remove_selected)
        btn_close.clicked.connect(self.close)
        act_row.addWidget(self.btn_remove)
        act_row.addWidget(btn_close)
        root.addLayout(act_row)

    def _populate(self):
        self.lst.clear()
        for name in sorted(load_tracked(), key=str.lower):
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            if not os.path.exists(os.path.join(WINDOWS_FONT_DIR, name)):
                item.setForeground(QColor("#888888"))
                item.setText(name + "  (file missing)")
            self.lst.addItem(item)

        if self.lst.count() == 0:
            self.log.append("no dafont-installed fonts found.")
            self.btn_remove.setEnabled(False)

    def _select_all(self):
        for i in range(self.lst.count()):
            self.lst.item(i).setCheckState(Qt.CheckState.Checked)

    def _deselect_all(self):
        for i in range(self.lst.count()):
            self.lst.item(i).setCheckState(Qt.CheckState.Unchecked)

    def _remove_selected(self):
        to_remove = [
            self.lst.item(i).text().replace("  (file missing)", "")
            for i in range(self.lst.count())
            if self.lst.item(i).checkState() == Qt.CheckState.Checked
        ]
        if not to_remove:
            self.log.append("nothing selected.")
            return

        reply = QMessageBox.question(
            self, "confirm removal",
            f"remove {len(to_remove)} font(s) from windows?\nthis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        for name in to_remove:
            uninstall_font(name, log_fn=lambda m: self.log.append(m))

        self.log.append(f"\ndone. removed {len(to_remove)} font(s).")
        self._populate()

# ─────────────────────────────────────────────────────────────────────────────
# download worker
# ─────────────────────────────────────────────────────────────────────────────

class DownloadWorker(QThread):
    log          = pyqtSignal(str)
    title_update = pyqtSignal(str)
    progress     = pyqtSignal(int)
    finished     = pyqtSignal()

    def __init__(self, settings: dict):
        super().__init__()
        self.settings         = settings
        self._paused          = False
        self._stopped         = False
        self.bytes_downloaded = 0
        self.max_bytes        = int(settings["max_gb"]) * 1024 ** 3

    def pause(self):  self._paused = True
    def resume(self): self._paused = False
    def stop(self):   self._stopped = True; self._paused = False

    def _check(self) -> bool:
        while self._paused:
            time.sleep(0.2)
        return self._stopped

    def run(self):
        main_dir     = self.settings["save_dir"]
        install      = self.settings["install_fonts"]
        delete_after = self.settings["delete_after"]
        letters      = "ABCDEFGHIJKLMNOPQRSTUVWXYZ#"

        os.makedirs(main_dir, exist_ok=True)

        for element in letters:
            if self._check():
                break
            letter_dir = os.path.join(main_dir, element)
            os.makedirs(letter_dir, exist_ok=True)

            try:
                url  = (f"https://www.dafont.com/alpha.php?lettre="
                        f"{element.lower()}&page=1&fpp=200")
                resp = SESSION.get(url, timeout=15)
                soup = BeautifulSoup(resp.text, "html.parser")
                nav  = soup.find("a", title="Keyboard shortcut: Right arrow")
                lastpage = int(nav.find_previous().text) if nav else 1
            except Exception as e:
                self.log.emit(f"[{element}] page-count error: {e}")
                lastpage = 1

            for page in range(1, lastpage + 1):
                if self._check():
                    break
                self.log.emit(f"[{element}] page {page}/{lastpage}")

                try:
                    url  = (f"https://www.dafont.com/alpha.php?lettre="
                            f"{element.lower()}&page={page}&fpp=200")
                    resp = SESSION.get(url, timeout=15)
                    soup = BeautifulSoup(resp.text, "html.parser")
                    divs = soup.find_all("div", {"class": "preview"})
                except Exception as e:
                    self.log.emit(f"  page error: {e}")
                    continue

                targets = []
                for div in divs:
                    try:
                        style = div["style"]
                        poster = (style
                                  .replace("background-image:url(/", "")
                                  .replace(")", ""))
                        slug = poster.replace(".png", "").rsplit("/", 1)[-1][:-1]
                        targets.append(slug)
                    except Exception:
                        pass

                with ThreadPoolExecutor(max_workers=8) as pool:
                    futures = {
                        pool.submit(
                            self._download_font,
                            slug, letter_dir, install, delete_after
                        ): slug
                        for slug in targets
                    }
                    for fut in as_completed(futures):
                        if self._check():
                            pool.shutdown(wait=False, cancel_futures=True)
                            break
                        size = fut.result()
                        if size:
                            self.bytes_downloaded += size
                            pct = min(100, int(
                                self.bytes_downloaded / self.max_bytes * 100))
                            self.progress.emit(pct)
                            if self.bytes_downloaded >= self.max_bytes:
                                self.log.emit("reached size limit — stopping.")
                                self.stop()
                                break

        self.title_update.emit("dafont downloader")
        self.finished.emit()

    def _download_font(self, slug: str, letter_dir: str,
                       install: bool, delete_after: bool) -> int:
        if self._stopped:
            return 0
        down_url = f"https://dl.dafont.com/dl/?f={slug}"
        try:
            t0   = time.time()
            resp = SESSION.get(down_url, timeout=30, stream=True)
            cd   = resp.headers.get("Content-Disposition", "")
            fname = (cd.split("filename=")[-1].strip().strip('"')
                     if "filename=" in cd else slug + ".zip")

            total      = int(resp.headers.get("Content-Length", 0))
            arch       = os.path.join(letter_dir, fname)
            downloaded = 0

            with open(arch, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=131072):
                    if self._stopped:
                        break
                    while self._paused:
                        time.sleep(0.2)
                    if chunk:
                        fh.write(chunk)
                        downloaded += len(chunk)
                        elapsed   = time.time() - t0
                        speed     = downloaded / elapsed if elapsed > 0 else 1
                        remaining = (total - downloaded) / speed if speed > 0 else 0
                        size_mb   = total / 1024 / 1024
                        self.title_update.emit(
                            f"dafont downloader  |  downloading: {slug}  "
                            f"|  size: {size_mb:.2f} mb  "
                            f"|  eta: {int(remaining)}s"
                        )

            if self._stopped:
                try:
                    os.remove(arch)
                except Exception:
                    pass
                return 0

            self.log.emit(f"  downloaded: {slug}")
            extract_and_clean(arch, letter_dir, install, delete_after,
                              log_fn=self.log.emit)
            return downloaded

        except Exception as e:
            self.log.emit(f"  error ({slug}): {e}")
            return 0

# ─────────────────────────────────────────────────────────────────────────────
# main window
# ─────────────────────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self.worker   = None
        self._build_ui()
        self._apply_settings()
        self.setWindowTitle("dafont downloader")
        if os.path.exists(ICON_FILE):
            self.setWindowIcon(QIcon(ICON_FILE))
        self.setMinimumSize(720, 620)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(10)
        root.setContentsMargins(14, 14, 14, 14)

        f = QFont()
        f.setPointSize(9)

        # admin badge
        self.admin_label = QLabel(
            "✔  running as administrator" if is_admin()
            else "⚠  not running as administrator — font install may fail"
        )
        self.admin_label.setFont(f)
        self.admin_label.setStyleSheet(
            "color: #2ecc71;" if is_admin() else "color: #e74c3c;"
        )
        root.addWidget(self.admin_label)

        # elevate button (only shown when not admin)
        if not is_admin():
            self.btn_elevate = QPushButton("restart as administrator")
            self.btn_elevate.setFont(f)
            self.btn_elevate.setFixedHeight(30)
            self.btn_elevate.setStyleSheet("color: #e74c3c; font-weight: bold;")
            self.btn_elevate.clicked.connect(relaunch_as_admin)
            root.addWidget(self.btn_elevate)

        # save location
        loc_box = QGroupBox("save location")
        loc_box.setFont(f)
        loc_lay = QHBoxLayout(loc_box)
        self.dir_edit = QLineEdit()
        self.dir_edit.setPlaceholderText("choose a folder…")
        browse_btn = QPushButton("browse")
        browse_btn.setFixedWidth(80)
        browse_btn.setFont(f)
        browse_btn.clicked.connect(self._browse)
        loc_lay.addWidget(self.dir_edit)
        loc_lay.addWidget(browse_btn)
        root.addWidget(loc_box)

        # size limit
        size_box = QGroupBox("size limit")
        size_box.setFont(f)
        size_lay = QVBoxLayout(size_box)
        self.size_label = QLabel("10 gb")
        self.size_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setMinimum(1)
        self.slider.setMaximum(56)
        self.slider.setValue(10)
        self.slider.setTickInterval(4)
        self.slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.slider.valueChanged.connect(lambda v: self.size_label.setText(f"{v} gb"))
        size_lay.addWidget(self.size_label)
        size_lay.addWidget(self.slider)
        root.addWidget(size_box)

        # options
        opt_box = QGroupBox("options")
        opt_box.setFont(f)
        opt_lay = QVBoxLayout(opt_box)
        self.chk_install = QCheckBox("automatically install fonts into windows")
        self.chk_install.setFont(f)
        self.chk_install.stateChanged.connect(self._on_install_toggled)
        self.chk_delete = QCheckBox("delete font file after installing")
        self.chk_delete.setFont(f)
        self.chk_delete.setVisible(False)
        opt_lay.addWidget(self.chk_install)
        opt_lay.addWidget(self.chk_delete)
        root.addWidget(opt_box)

        # progress
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%  of size limit")
        root.addWidget(self.progress_bar)

        # buttons
        btn_lay = QHBoxLayout()
        self.btn_start  = QPushButton("start")
        self.btn_pause  = QPushButton("pause")
        self.btn_stop   = QPushButton("stop")
        self.btn_remove = QPushButton("remove fonts")
        for b in (self.btn_start, self.btn_pause, self.btn_stop, self.btn_remove):
            b.setFixedHeight(34)
            b.setFont(f)
            btn_lay.addWidget(b)
        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.btn_start.clicked.connect(self._start)
        self.btn_pause.clicked.connect(self._pause)
        self.btn_stop.clicked.connect(self._stop)
        self.btn_remove.clicked.connect(self._open_remove_window)
        root.addLayout(btn_lay)

        # log
        log_box = QGroupBox("log")
        log_box.setFont(f)
        log_lay = QVBoxLayout(log_box)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont("Consolas", 8))
        self.log_view.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        log_lay.addWidget(self.log_view)
        root.addWidget(log_box)

    def _apply_settings(self):
        self.dir_edit.setText(self.settings.get("save_dir", ""))
        self.slider.setValue(int(self.settings.get("max_gb", 10)))
        self.chk_install.setChecked(bool(self.settings.get("install_fonts", False)))
        self.chk_delete.setChecked(bool(self.settings.get("delete_after", False)))
        self.chk_delete.setVisible(self.chk_install.isChecked())

    def _collect_settings(self) -> dict:
        return {
            "save_dir":      self.dir_edit.text().strip() or "C:/fonts/dafonts/",
            "max_gb":        self.slider.value(),
            "install_fonts": self.chk_install.isChecked(),
            "delete_after":  self.chk_delete.isChecked(),
        }

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "select save folder",
                                             self.dir_edit.text())
        if d:
            self.dir_edit.setText(d)

    def _on_install_toggled(self, state):
        self.chk_delete.setVisible(bool(state))

    def _log(self, msg: str):
        self.log_view.append(msg)
        self.log_view.verticalScrollBar().setValue(
            self.log_view.verticalScrollBar().maximum())

    def _set_title(self, t: str):
        self.setWindowTitle(t.lower())

    def _set_progress(self, pct: int):
        self.progress_bar.setValue(pct)

    def _on_finished(self):
        self._log("done.")
        self.btn_start.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.btn_pause.setText("pause")

    def _open_remove_window(self):
        RemoveFontsWindow(self).exec()

    def _start(self):
        s = self._collect_settings()
        save_settings(s)
        self.settings = s
        self.log_view.clear()
        self.progress_bar.setValue(0)

        self.worker = DownloadWorker(s)
        self.worker.log.connect(self._log)
        self.worker.title_update.connect(self._set_title)
        self.worker.progress.connect(self._set_progress)
        self.worker.finished.connect(self._on_finished)
        self.worker.start()

        self.btn_start.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_stop.setEnabled(True)

    def _pause(self):
        if not self.worker:
            return
        if self.worker._paused:
            self.worker.resume()
            self.btn_pause.setText("pause")
            self._log("resumed.")
        else:
            self.worker.pause()
            self.btn_pause.setText("resume")
            self._log("paused.")

    def _stop(self):
        if self.worker:
            self.worker.stop()
            self._log("stopping…")
        self.btn_start.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.btn_pause.setText("pause")

    def closeEvent(self, event):
        save_settings(self._collect_settings())
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(3000)
        event.accept()

# ─────────────────────────────────────────────────────────────────────────────
# entry
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    if os.path.exists(ICON_FILE):
        app.setWindowIcon(QIcon(ICON_FILE))
    win = MainWindow()
    win.show()
    sys.exit(app.exec())