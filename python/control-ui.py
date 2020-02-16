#!/usr/bin/env python

#import pathlib
import time
import logging
import gi
gi.require_version('WebKit2', '4.0')
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk
from gi.repository.WebKit2 import WebView, Settings
#gi.require_version('WebKit2', '4.0')
#from gi.repository import WebKit2 as Webkit


# setup logging
lg = logging.getLogger('control-ui')
lg.setLevel(logging.DEBUG)
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)
formatter = logging.Formatter(('%(asctime)s|%(name)s|%(levelname)s|'
                               '%(message)s'))
ch.setFormatter(formatter)
lg.addHandler(ch)

lg.info(f'WTF')

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

window = builder.get_object("mainWindow")
#hb = builder.get_object("headerBar")
#window.set_titlebar(hb)

window.show_all()
#time.sleep(10)

Gtk.main()
