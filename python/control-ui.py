#!/usr/bin/env python

#import pathlib
#import time
#import signal # to handle key kill
import logging
import systemd.journal
#import logging.handlers
import gi
import sys
#import os
#os.environ["DEBUSSY"] = "1"

#gi.require_version('WebKit2', '4.0')
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import GLib, Gio, Gtk
#Gdk.set_allowed_backends('broadway')
#from gi.repository.WebKit2 import WebView, Settings
#gi.require_version('WebKit2', '4.0')
#from gi.repository import WebKit2 as Webkit


# setup logging
lg = logging.getLogger('control-ui')
lg.setLevel(logging.DEBUG)
sysL = systemd.journal.JournalHandler(SYSLOG_IDENTIFIER=lg.name)
ch = logging.StreamHandler()
logFormat = logging.Formatter(('%(asctime)s|%(name)s|%(levelname)s|'
                               '%(message)s'))
uiLogFormat = logging.Formatter(('%(asctime)s|%(levelname)s|%(message)s'))
sysLogFormat = logging.Formatter(('%(levelname)s|%(message)s'))
sysL.setFormatter(sysLogFormat)
ch.setFormatter(logFormat)
lg.addHandler(ch)
lg.addHandler(sysL)

books = [["Tolstoy, Leo", ["War and Peace", True], ["Anna Karenina", False]],
         ["Shakespeare, William", ["Hamlet", False],
             ["Macbeth", True], ["Othello", False]],
         ["Tolkien, J.R.R.", ["The Lord of the Rings", False]]]

#Webkit.WebView()

s = 20
d = 6

# the data are stored in the model
# create a treestore with two columns
devStore = Gtk.TreeStore(str, bool)
# fill in the model
for i in range(len(books)):
    # the iter piter is returned when appending the author in the first column
    # and False in the second
    piter = devStore.append(None, [books[i][0], False])
    # append the books and the associated boolean value as children of
    # the author
    j = 1
    while j < len(books[i]):
        devStore.append(piter, books[i][j])
        j += 1


#devTV.set_model(devStore)


renderer_books = Gtk.CellRendererText()
# the first column is created
column_books = Gtk.TreeViewColumn("Books", renderer_books, text=0)
# and it is appended to the treeview


#devTV.append_column(column_books)

# the cellrenderer for the second column - boolean rendered as a toggle
renderer_in_out = Gtk.CellRendererToggle()
# the second column is created
column_in_out = Gtk.TreeViewColumn("Out?", renderer_in_out, active=1)
# and it is appended to the treeview



#devTV.append_column(column_in_out)

# callback function for the signal emitted by the cellrenderertoggle
def on_toggled(widget, path):
    # the boolean value of the selected row
    current_value = devStore[path][1]
    # change the boolean value of the selected row in the model
    devStore[path][1] = not current_value
    # new current value!
    current_value = not current_value
    # if length of the path is 1 (that is, if we are selecting an author)
    if len(path) == 1:
        # get the iter associated with the path
        piter = devStore.get_iter(path)
        # get the iter associated with its first child
        citer = devStore.iter_children(piter)
        # while there are children, change the state of their boolean value
        # to the value of the author
        while citer is not None:
            devStore[citer][1] = current_value
            citer = devStore.iter_next(citer)
    # if the length of the path is not 1 (that is, if we are selecting a
    # book)
    elif len(path) != 1:
        # get the first child of the parent of the book (the first book of
        # the author)
        citer = devStore.get_iter(path)
        piter = devStore.iter_parent(citer)
        citer = devStore.iter_children(piter)
        # check if all the children are selected
        all_selected = True
        while citer is not None:
            if devStore[citer][1] == False:
                all_selected = False
                break
            citer = devStore.iter_next(citer)
        # if they do, the author as well is selected; otherwise it is not
        devStore[piter][1] = all_selected


# connect the cellrenderertoggle with a callback function
renderer_in_out.connect("toggled", on_toggled)



class App(Gtk.Application):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, application_id="net.christoforo.control-ui",
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
                         **kwargs)
        self.main_win = None
        self.b = None
        self.numPix = 6
        self.numSubstrates = 20

        self.add_main_option("test", ord("t"), GLib.OptionFlags.NONE,
                             GLib.OptionArg.NONE, "Command line test", None)

    def do_startup(self):
        lg.debug("Starting up app")
        Gtk.Application.do_startup(self)

        self.b = Gtk.Builder()
        self.b.add_from_file("ui.glade")

        self.labelTree = self.b.get_object("labelTree")
        self.deviceTree = self.b.get_object("devTV")
        self.deviceTree.set_model(devStore)
        self.deviceTree.append_column(column_books)
        self.deviceTree.append_column(column_in_out)

        self.labelStore = Gtk.TreeStore(str, bool, str, bool, str, int)
        for i in range(self.numSubstrates):
            # the iter piter is returned when appending the author
            self.labelStore.append(None, [f"{i+1}", False, "", True, "Missing substrate label", 0])

        self.labelTree.set_model(self.labelStore)
        # the cellrenderer for the column - text
        renderText = Gtk.CellRendererText()
        # the column is created
        numbers = Gtk.TreeViewColumn("Substrate", renderText, text=0, editable=1)
        # and it is appended to the treeview
        self.labelTree.append_column(numbers)
        labels = Gtk.TreeViewColumn("Label", renderText, text=2, editable=3, placeholder_text=4, ypad=5)
        self.labelTree.append_column(labels)



        self.b.connect_signals(self)  # maps all ui callbacks to functions here

    def do_activate(self):
        lg.debug("Activating app")

        # We only allow a single window and raise any existing ones
        if self.main_win is None:
            # Windows are associated with the application
            # when the last one is closed the application shuts down
            self.logTB = self.b.get_object("tbLog")  # log text buffer
            self.ltv = self.b.get_object("ltv")  # log text view

            def myWrite(buf):
                self.logTB.insert(self.logTB.get_end_iter(), str(buf))
                self.ltv.scroll_to_iter(self.logTB.get_end_iter(), 0, False, 0, 0)

            def myFlush():
                pass

            self.logTB.write = myWrite
            self.logTB.flush = myFlush
            uiLog = logging.StreamHandler(stream=self.logTB)
            uiLog.set_name('ui')
            uiLog.setFormatter(uiLogFormat)
            lg.addHandler(uiLog)
            lg.debug("Gui logging setup.")

            self.main_win = self.b.get_object("mainWindow")
            self.main_win.set_application(self)

        self.main_win.present()

    def do_command_line(self, command_line):
        lg.debug("Doing command line things")
        options = command_line.get_options_dict()
        # convert GVariantDict -> GVariant -> dict
        options = options.end().unpack()

        if "test" in options:
            # This is printed on the main instance
            lg.debug('Test argument recieved: {options["test"]}')

        self.activate()
        return 0

    def on_about(self, action, param):
        about_dialog = Gtk.AboutDialog(transient_for=self.main_win, modal=True)
        about_dialog.show()

    def do_shutdown(self):
        # remove gui log handler
        for h in lg.handlers:
            if h.get_name() == 'ui':
                lg.removeHandler(h)
                lg.debug("Shutting down")
        Gtk.Application.do_shutdown(self)

    def on_runCodeButton_clicked(self, button):
        lg.debug("Hello World!")

    def on_iv_devs_icon_release(self, icon, a, b):
        lg.debug("devs icon released")
        dd = self.b.get_object("devDialog")
        dd.present()


if __name__ == "__main__":
    app = App()
    app.run(sys.argv)
