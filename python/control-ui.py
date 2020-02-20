#!/usr/bin/env python

#import pathlib
#import time
#import signal # to handle key kill
import logging
import systemd.journal
#import logging.handlers
import gi
import os
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
logFormat = logging.Formatter(('%(asctime)s|%(name)s|%(levelname)s|'
                               '%(message)s'))
uiLogFormat = logging.Formatter(('%(asctime)s|%(levelname)s|%(message)s'))
sysLogFormat = logging.Formatter(('%(levelname)s|%(message)s'))
sysL.setFormatter(sysLogFormat)
ch.setFormatter(logFormat)
lg.addHandler(ch)
lg.addHandler(sysL)

#Webkit.WebView()





builder = Gtk.Builder()
builder.add_from_file("ui.glade")

class Handler:
    def on_mainWindow_destroy(self, *args):
        lg.debug("Destroying")
        Gtk.main_quit()

    def on_runCodeButton_clicked(self, button):
        lg.debug("Hello World!")

    def on_iv_devs_icon_release(self, icon, a, b):
        lg.debug("devs icon released")
        dd = builder.get_object("devDialog")
        dd.show_all()


builder.connect_signals(Handler())
logTB = builder.get_object("tbLog")
ltv = builder.get_object("ltv")
tv = builder.get_object("tv")
devTV = builder.get_object("devTV")

numSubstrates = 20
numPix = 6

# make the store
store = Gtk.TreeStore(str, bool, str, bool, str, int)
for i in range(numSubstrates):
    # the iter piter is returned when appending the author
    store.append(None, [f"{i+1}", False, "", True, "Missing substrate label", 5])


tv.set_model(store)
# the cellrenderer for the column - text
renderText = Gtk.CellRendererText()
# the column is created
numbers = Gtk.TreeViewColumn("Substrate", renderText, text=0, editable=1)
# and it is appended to the treeview
tv.append_column(numbers)
labels = Gtk.TreeViewColumn("Label", renderText, text=2, editable=3, placeholder_text=4, ypad=5)
tv.append_column(labels)


books = [["Tolstoy, Leo", ["War and Peace", True], ["Anna Karenina", False]],
         ["Shakespeare, William", ["Hamlet", False],
             ["Macbeth", True], ["Othello", False]],
         ["Tolkien, J.R.R.", ["The Lord of the Rings", False]]]

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
devTV.set_model(devStore)
renderer_books = Gtk.CellRendererText()
# the first column is created
column_books = Gtk.TreeViewColumn("Books", renderer_books, text=0)
# and it is appended to the treeview
devTV.append_column(column_books)

# the cellrenderer for the second column - boolean rendered as a toggle
renderer_in_out = Gtk.CellRendererToggle()
# the second column is created
column_in_out = Gtk.TreeViewColumn("Out?", renderer_in_out, active=1)
# and it is appended to the treeview
devTV.append_column(column_in_out)

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



class Application(Gtk.Application):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, application_id="net.christoforo.control-ui",
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
                         **kwargs)
        self.window = None

        self.add_main_option("test", ord("t"), GLib.OptionFlags.NONE,
                             GLib.OptionArg.NONE, "Command line test", None)

    def do_startup(self):
        Gtk.Application.do_startup(self)

        self.builder = Gtk.Builder()
        self.builder.add_from_file("ui.glade")

    def do_activate(self):
        # We only allow a single window and raise any existing ones
        if self.window is None:
            # Windows are associated with the application
            # when the last one is closed the application shuts down
            self.window = AppWindow(application=self, title="Main Window")

        self.window.present()

    def do_command_line(self, command_line):
        options = command_line.get_options_dict()
        # convert GVariantDict -> GVariant -> dict
        options = options.end().unpack()

        if "test" in options:
            # This is printed on the main instance
            print("Test argument recieved: %s" % options["test"])

        self.activate()
        return 0

    def on_about(self, action, param):
        about_dialog = Gtk.AboutDialog(transient_for=self.window, modal=True)
        about_dialog.present()

    def on_quit(self, action, param):
        self.quit()

if __name__ == "__main__":
    app = Application()
    app.run(sys.argv)