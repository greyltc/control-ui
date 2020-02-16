#!/usr/bin/python
import gi
gi.require_version('WebKit2', '4.0')
from gi.repository import WebKit2
from gi.repository import Gtk

def close(window):
        Gtk.main_quit()

def main():
        Gtk.init()

        view = WebKit2.WebView()
        view.load_uri("http://google.es")
        window = Gtk.Window()
        #window.add(view)
        window.connect("destroy", close)
        window.show_all()

        Gtk.main()

main()
