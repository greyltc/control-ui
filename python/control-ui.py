#!/usr/bin/env python

#import pathlib
#import time
#import signal to handle key kill
import logging
import systemd.journal
#import logging.handlers
import gi
#import os
#os.environ["DEBUSSY"] = "1"

#gi.require_version('WebKit2', '4.0')
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gtk, Gdk
#Gdk.set_allowed_backends('broadway')
#from gi.repository.WebKit2 import WebView, Settings
#gi.require_version('WebKit2', '4.0')
#from gi.repository import WebKit2 as Webkit


# setup logging
lg = logging.getLogger('control-ui')
lg.setLevel(logging.DEBUG)
sysL = systemd.journal.JournalHandler(SYSLOG_IDENTIFIER=lg.name)
ch = logging.StreamHandler()
#ch.setLevel(logging.DEBUG)
logFormat = logging.Formatter(('%(asctime)s|%(name)s|%(levelname)s|'
                               '%(message)s'))
uiLogFormat = logging.Formatter(('%(asctime)s|%(levelname)s|%(message)s'))
sysLogFormat = logging.Formatter(('%(levelname)s|%(message)s'))
sysL.setFormatter(sysLogFormat)
ch.setFormatter(logFormat)
lg.addHandler(ch)
lg.addHandler(sysL)

#Webkit.WebView()


class Handler:
    def on_mainWindow_destroy(self, *args):
        lg.debug("Destroying")
        Gtk.main_quit()

    def on_runCodeButton_clicked(self, button):
        lg.debug("Hello World!")


builder = Gtk.Builder()
builder.add_from_file("ui.glade")
builder.connect_signals(Handler())
logTB = builder.get_object("tbLog")
ltv = builder.get_object("ltv")


def myWrite(buf):
    logTB.insert(logTB.get_end_iter(), str(buf))
    ltv.scroll_to_iter(logTB.get_end_iter(), 0, False, 0, 0)


def myFlush():
    pass


logTB.write = myWrite
logTB.flush = myFlush
uiLog = logging.StreamHandler(stream=logTB)
uiLog.setFormatter(uiLogFormat)
lg.addHandler(uiLog)
lg.debug("Logging started.")

window = builder.get_object("mainWindow")
window.show_all()
Gtk.main()
