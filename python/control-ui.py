#!/usr/bin/env python

import pathlib

# import time
# import signal # to handle key kill
import logging
import systemd.journal

# import logging.handlers
import gi
import sys
import os
import time
import math
import humanize
import datetime as dt
import paho.mqtt.client as mqtt
import configparser
import json
import pickle

# os.environ["DEBUSSY"] = "1"

gi.require_version("WebKit2", "4.0")
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import GLib, Gio, Gtk, Gdk, Pango

# Gdk.set_allowed_backends('broadway')  # for gui over web
from gi.repository.WebKit2 import WebView, Settings

# setup logging
lg = logging.getLogger("control-ui")
lg.setLevel(logging.DEBUG)
sysL = systemd.journal.JournalHandler(SYSLOG_IDENTIFIER=lg.name)
ch = logging.StreamHandler()
logFormat = logging.Formatter(("%(asctime)s|%(name)s|%(levelname)s|%(message)s"))
uiLogFormat = logging.Formatter(("%(asctime)s|%(levelname)s|%(message)s"))
sysLogFormat = logging.Formatter(("%(levelname)s|%(message)s"))
sysL.setFormatter(sysLogFormat)
ch.setFormatter(logFormat)
lg.addHandler(ch)
lg.addHandler(sysL)


# Webkit.WebView()


class App(Gtk.Application):
    # list of all widget id srings
    # ids = [
    #     "eqe_in",
    #     "eqe_int",
    #     "eqedevbias",
    #     "firstEnd",
    #     "firstStart",
    #     "gblc",
    #     "iscdwell",
    #     "mpptTime",
    #     "nmStart",
    #     "nmStep",
    #     "nmStop",
    #     "nmStop1",
    #     "nmWidth",
    #     "nplc",
    #     "rblc",
    #     "secondEnd",
    #     "secondStart",
    #     "sweepDelay",
    #     "sweepSteps",
    #     "vocdwell",
    #     "run_name_prefix",
    #     "iv_devs",
    #     "autoiv",
    #     "eqe_devs",
    # ]

    def __init__(self, *args, **kwargs):
        """Constructor."""
        self.cl_config = pathlib.Path()
        config_file_name = "measurement_config.ini"
        self.config_file = pathlib.Path(config_file_name)
        super().__init__(
            *args,
            application_id="net.christoforo.control-ui",
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
            **kwargs,
        )

        # allow configuration file location to be specified by command line argument
        self.add_main_option(
            "config",
            ord("c"),
            GLib.OptionFlags.NONE,
            GLib.OptionArg.FILENAME,
            "Configuration file",
            None,
        )

        self.config = configparser.ConfigParser(
            interpolation=configparser.ExtendedInterpolation()
        )

        # let's figure out where the configuration file is
        config_env_var = "MEASUREMENT_CONFIGURATION_FILE_NAME"
        if config_env_var in os.environ:
            env_config = pathlib.Path(os.environ.get(config_env_var))
        else:
            env_config = pathlib.Path()
        home_config = pathlib.Path.home() / config_file_name
        local_config = pathlib.Path(config_file_name)
        local_python_config = pathlib.Path("python") / config_file_name
        config_ini = pathlib.Path("config.ini")
        self.config_warn = False
        if self.cl_config.is_file():  # priority 1: check the command line
            lg.debug("Using config file from command line")
            self.config_file = self.cl_config
        elif env_config.is_file():  # priority 2: check the environment
            lg.debug(f"Using config file from {config_env_var} variable")
            self.config_file = env_config
        elif home_config.is_file():  # priority 3: check in home dir
            lg.debug(
                f"Using config file {config_file_name} in home dir: {pathlib.Path.home()}"
            )
            self.config_file = home_config
        elif local_config.is_file():  # priority 4: check in the current drectory
            lg.debug(f"Using local config file {local_config.resolve()}")
            self.config_file = local_config
        elif local_python_config.is_file():  # priority 5: check in python/
            lg.debug(f"Using local config file {local_python_config.resolve()}")
            self.config_file = local_python_config
            self.config_warn = True
        elif config_ini.is_file():  # priority 6: check for ./config.ini
            lg.debug(f"Using local config file {config_ini.resolve()}")
            self.config_file = config_ini
        else:  # and give up
            lg.error("Unable to find a configuration file to load.")
            raise (ValueError("No config file"))

        try:
            lg.debug(f"Parsing {self.config_file.resolve()}")
            with open(self.config_file, "r") as f:
                for line in f:
                    lg.debug(line.rstrip())
            self.config.read(str(self.config_file))
        except:
            lg.error("Unexpected error parsing config file.")
            lg.error(sys.exc_info()[0])
            raise


        # get dimentions of substrate array to generate designators
        number_list = [int(x) for x in self.config["substrates"]["number"].split(",")]
        self.substrate_designators = self._generate_substrate_designators(number_list)
        self.numSubstrates = len(self.substrate_designators)

        self.main_win = None
        self.b = None
        self.ids = []  # ids of objects to save/load
        self.numPix = len(
            self.config[self.config["substrates"]["active_layout"]]["pixels"].split(",")
        )
        self.approx_seconds_per_iv = 50
        self.approx_seconds_per_eqe = 150
        self.live_data_uri = self.config["network"]["live_data_uri"]

        galde_ui_xml_file_name = "ui.glade"
        gui_file = pathlib.Path(galde_ui_xml_file_name)
        if gui_file.is_file():
            self.galde_ui_xml_file = str(gui_file)
        elif ("python" / gui_file).is_file():  # for debugging
            self.galde_ui_xml_file = str("python" / gui_file)
        else:
            raise (ValueError("Can't find glade file!"))

        # start MQTT client
        self._start_mqtt()

    def _generate_substrate_designators(self, number_list):
        """Generate a list of substrate designators.

        Parameters
        ----------
        number_list : list
            List of numbers of substrates along each available axis. Length must be
            1 or 2.
        """
        rs = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"[: number_list[0]]

        try:
            cs = range(number_list[1])
        except IndexError:
            # if number of columns not given, must be 1
            cs = range(1)

        subdes = [f"{m}{n + 1}" for m in rs for n in cs]

        return subdes

    def _start_mqtt(self):
        """Start the MQTT client and subscribe to the CLI topic."""

        def on_message(mqttc, obj, msg):
            """Act on an MQTT message."""
            m = json.loads(msg.payload)
            lg.debug(f"New message: {m}")

            if (subtopic := msg.topic.split("/")[-1]) == "stage_pos":
                self.b.get_object("goto_x").set_text(m[0])
                self.b.get_object("goto_y").set_text(m[0])

        # connect to mqtt broker
        self.MQTTHOST = self.config["network"]["MQTTHOST"]
        self.mqttc = mqtt.Client()
        self.mqttc.on_message = on_message
        self.mqttc.connect(self.MQTTHOST)
        # subscribe to cli topic to report back on progress
        self.mqttc.subscribe("cli/#", qos=2)
        self.mqttc.loop_start()

    def _stop_mqtt(self):
        """Stop the MQTT client."""
        self.mqttc.loop_stop()
        self.mqttc.disconnect()

    def do_startup(self):
        lg.debug("Starting up app")
        Gtk.Application.do_startup(self)

        self.b = Gtk.Builder()
        self.b.add_from_file(self.galde_ui_xml_file)

        # crawl the builder object and mine all the object ID strings we can
        # this list of id strings will need to be filtered by the load and save functions
        # a good starting point will be to discard those starting with "___"
        # I should probably just rename all the saveable/loadable stuff to contain
        # some special character so the filtering becomes easier
        for o in self.b.get_objects():
            try:
                self.ids.append(Gtk.Buildable.get_name(o))
            except:
                pass

        self.label_tree, self.label_store = self.setup_label_tree()

        self.dev_store = [
            Gtk.TreeStore(str, bool, bool),
            Gtk.TreeStore(str, bool, bool),
        ]  # [iv devs, eqe devs]
        self.setup_dev_stores()

        self.dev_tree = self.b.get_object("devTV")
        self.setup_dev_tree(self.dev_tree)

        # TODO: do this in a non-obsolete way (probably w/ css)
        fontdesc = Pango.FontDescription("monospace")

        max_devices = self.numPix * self.numSubstrates
        address_string_length = math.ceil(max_devices / 4)
        selection_box_length = 4 + address_string_length + 3
        default_on = "0x" + "F" * address_string_length
        default_off = "0x" + "0" * address_string_length

        self.def_fmt_str = f"0{address_string_length}X"

        self.iv_dev_box = self.b.get_object("iv_devs")

        self.last_valid_devs = [default_on, default_off]  # [iv devs, eqe devs]

        self.iv_dev_box = self.b.get_object("iv_devs")
        self.iv_dev_box.modify_font(fontdesc)
        self.iv_dev_box.set_text(default_on)
        self.iv_dev_box.set_width_chars(selection_box_length)
        self.iv_dev_box.set_icon_from_icon_name(0, "emblem-default")

        self.eqe_dev_box = self.b.get_object("eqe_devs")
        self.eqe_dev_box.modify_font(fontdesc)
        self.eqe_dev_box.set_text(default_off)
        self.eqe_dev_box.set_width_chars(selection_box_length)
        self.eqe_dev_box.set_icon_from_icon_name(0, "emblem-default")

        self.po = self.b.get_object("picker_po")
        self.po.set_position(Gtk.PositionType.BOTTOM)

        self.tick()
        self.ticker = self.timeout_id = GLib.timeout_add_seconds(1, self.tick, None)
        self.b.connect_signals(self)  # maps all ui callbacks to functions here

        wv = self.b.get_object("wv")
        if self.live_data_uri != "":
            wv.load_uri(self.live_data_uri)

        # send config file to CLI MQTT client
        with open(self.config_file, "r") as f:
            payload = json.dumps(f.read())
        self.mqttc.publish("gui/config", payload, qos=2).wait_for_publish()

    def setup_dev_stores(self):
        for store in self.dev_store:
            checked = True
            inconsistent = False
            # fill in the model
            for i in range(self.numSubstrates):
                if self.label_store[i][0] == "":
                    label = self.label_store[i][1]
                else:
                    label = self.label_store[i][0]
                piter = store.append(None, [label, checked, inconsistent])
                j = 1
                while j <= self.numPix:
                    store.append(piter, [f"Device {j}", checked, inconsistent])
                    j += 1

    def setup_dev_tree(self, deviceTree):
        # deviceTree.set_model(devStore)
        renderDesignator = Gtk.CellRendererText()
        # the first column is created
        designator = Gtk.TreeViewColumn("Substrate/Device", renderDesignator, text=0)
        deviceTree.append_column(designator)

        # the cellrenderer for the second column - boolean rendered as a toggle
        renderCheck = Gtk.CellRendererToggle()
        # the second column is created
        colCheck = Gtk.TreeViewColumn("Measure?", renderCheck, active=1, inconsistent=2)
        deviceTree.append_column(colCheck)

        # connect the cellrenderertoggle with a callback function
        renderCheck.connect("toggled", self.dev_toggle)

        deviceTree.connect("key-release-event", self.handle_dev_key)

    # callback function for select/deselect device/substrate
    def dev_toggle(self, toggle, path):
        eqe = "eqe" in Gtk.Buildable.get_name(self.po.get_relative_to())
        path_split = path.split(":")
        # the boolean value of the selected row
        current_value = self.dev_store[eqe][path][1]
        # change the boolean value of the selected row in the model
        self.dev_store[eqe][path][1] = not current_value
        # new current value!
        current_value = not current_value
        # if length of the path is 1 (that is, if we are selecting a substrate)
        if len(path_split) == 1:
            # get the iter associated with the path
            piter = self.dev_store[eqe].get_iter(path)
            self.dev_store[eqe][piter][2] = False
            # get the iter associated with its first child
            citer = self.dev_store[eqe].iter_children(piter)
            # while there are children, change the state of their boolean value
            # to the value of the substrate
            while citer is not None:
                self.dev_store[eqe][citer][1] = current_value
                citer = self.dev_store[eqe].iter_next(citer)
        # if the length of the path is not 1 (that is, if we are selecting a
        # device)
        elif len(path_split) != 1:
            # get the first child of the parent of the substrate (device 1)
            citer = self.dev_store[eqe].get_iter(path)
            piter = self.dev_store[eqe].iter_parent(citer)
            citer = self.dev_store[eqe].iter_children(piter)
            # check if all the children are selected
            num_selected = 0
            while citer is not None:
                if self.dev_store[eqe][citer][1] is True:
                    num_selected = num_selected + 1
                citer = self.dev_store[eqe].iter_next(citer)
            # if they do, the device as well is selected; otherwise it is not
            if num_selected == self.numPix:
                self.dev_store[eqe][piter][2] = False
                self.dev_store[eqe][piter][1] = True
            elif num_selected == 0:
                self.dev_store[eqe][piter][2] = False
                self.dev_store[eqe][piter][1] = False
            else:
                self.dev_store[eqe][piter][2] = True

        # iterate through everything and build up the result
        siter = self.dev_store[eqe].get_iter("0")  # substrate iterator
        selection_bitmask = 0
        bit_location = 0
        while siter is not None:
            diter = self.dev_store[eqe].iter_children(siter)  # device iterator
            while diter is not None:
                if self.dev_store[eqe][diter][1] is True:
                    selection_bitmask = selection_bitmask + (1 << bit_location)
                bit_location = bit_location + 1
                diter = self.dev_store[eqe].iter_next(
                    diter
                )  # advance to the next device
            siter = self.dev_store[eqe].iter_next(
                siter
            )  # advance to the next substrate

        if eqe:
            self.eqe_dev_box.set_text(f"0x{selection_bitmask:{self.def_fmt_str}}")
        else:
            self.iv_dev_box.set_text(f"0x{selection_bitmask:{self.def_fmt_str}}")

    # change in a device text box value
    def on_devs_changed(self, editable, user_data=None):
        eqe = "eqe" in Gtk.Buildable.get_name(editable)
        valid = False
        text_is = editable.get_text()
        if len(text_is) == len(f"0x{0:{self.def_fmt_str}}"):
            try:
                is_upper = text_is.upper()
                if is_upper[0:2] == "0X":
                    num_part = is_upper[2::]
                    valid_chars = "0123456789ABCDEF"
                    filtered = filter(lambda ch: ch in valid_chars, num_part)
                    num_part = "".join(filtered)
                    selection_bitmask = int(num_part, 16)
                    should_be = f"0x{selection_bitmask:{self.def_fmt_str}}"
                    if text_is == should_be:
                        valid = True
            except:
                pass

        if valid is True:
            self.last_valid_devs[eqe] = text_is
            editable.set_icon_from_icon_name(0, "emblem-default")
            if eqe:
                self.measure_note(selection_bitmask, self.approx_seconds_per_eqe)
            else:
                self.measure_note(selection_bitmask, self.approx_seconds_per_iv)
        else:
            editable.set_icon_from_icon_name(0, "dialog-error")

    def on_devs_focus_out_event(self, widget, user_data=None):
        eqe = "eqe" in Gtk.Buildable.get_name(widget)
        text_is = widget.get_text()
        try:
            selection_bitmask = int(text_is, 16)
            should_be = f"0x{selection_bitmask:{self.def_fmt_str}}"
            if text_is == should_be:
                return
            widget.set_text(should_be)
            self.last_valid_devs[eqe] = should_be
        except:
            widget.set_text(self.last_valid_devs[eqe])
            lg.warn(f"Bad device selection reverted")
        widget.set_icon_from_icon_name(0, "emblem-default")

    # log message printer for device selection change
    def measure_note(self, selection_bitmask, seconds_per):
        num_selected = sum([c == "1" for c in bin(selection_bitmask)])
        duration_string = humanize.naturaldelta(
            dt.timedelta(seconds=seconds_per * num_selected)
        )
        lg.info(f"{num_selected} devices selected for ~ {duration_string}")

    def setup_label_tree(self):
        labelTree = self.b.get_object("labelTree")
        labelStore = Gtk.ListStore(str, str, int)
        y_pad = 0
        label = ""
        for i in range(self.numSubstrates):
            # the iter piter is returned when appending the author
            # designator = self.substrate_designators[i]
            labelStore.append([label, self.substrate_designators[i], y_pad])
        labelTree.set_model(labelStore)

        # the uneditable substrate designator col
        # renderText = Gtk.CellRendererText()
        # numbers = Gtk.TreeViewColumn("Substrate", renderText, text=0, ypad=3)
        # labelTree.append_column(numbers)

        # the editable substrate label col
        renderEdit = Gtk.CellRendererText()
        renderEdit.set_property("editable", True)
        labels = Gtk.TreeViewColumn(
            "Substrate Label", renderEdit, text=0, placeholder_text=1, ypad=2
        )
        labelTree.append_column(labels)

        renderEdit.connect("edited", self.store_substrate_label)
        labelTree.connect("key-release-event", self.handle_label_key)

        return (labelTree, labelStore)

    # handles keystroke in the label creation tree
    def handle_label_key(self, tv, event):
        keyname = Gdk.keyval_name(event.keyval)
        if keyname in ["Return", "Enter"]:
            path, col = self.label_tree.get_cursor()
            path.next()
            self.label_tree.set_cursor_on_cell(
                path, focus_column=col, focus_cell=None, start_editing=True
            )

    # handles keystroke in the device selection tree
    def handle_dev_key(self, tv, event):
        # eqe = 'eqe' in Gtk.Buildable.get_name(self.po.get_relative_to())
        keyname = Gdk.keyval_name(event.keyval)
        if keyname in ["Right", "Left"]:
            path, col = self.dev_tree.get_cursor()
            if self.dev_tree.row_expanded(path) is True:
                self.dev_tree.collapse_row(path)
            else:
                self.dev_tree.expand_row(path, False)

    def store_substrate_label(self, widget, path, text):
        self.label_store[path][0] = text
        if text == "":
            self.dev_store[0][path][0] = self.label_store[path][1]
            self.dev_store[1][path][0] = self.label_store[path][1]
        else:
            self.dev_store[0][path][0] = self.label_store[path][0]
            self.dev_store[1][path][0] = self.label_store[path][0]

    # handle the auto iv toggle
    def on_autoiv_toggled(self, button, user_data=None):
        siblings = button.get_parent().get_children()
        siblings.remove(button)
        ali = siblings[0]

        if button.get_active():
            sensitivity = False
        else:
            sensitivity = True

        ali.set_sensitive = sensitivity
        for child in ali.get_children():
            child.set_sensitive(sensitivity)
            for grandchild in child.get_children():
                grandchild.set_sensitive(sensitivity)

    # runs once per second
    def tick(self, user_data=None):
        # lg.debug("tick")
        if self.mqttc.is_connected():
            status = "Connected"
        else:
            status = "Disconnected"
        self.b.get_object("headerBar").set_subtitle(f"Status: {status}")
        rns = self.b.get_object("run_name_suffix")
        now = int(time.time())
        rns.set_text(str(now))
        self.update_run_name()
        return True

    def update_run_name(self, user_data=None):
        rnp = self.b.get_object("run_name_prefix")
        rn = self.b.get_object("run_name")
        rns = self.b.get_object("run_name_suffix")
        rn.set_text(rnp.get_text() + rns.get_text())

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
                self.ltv.scroll_to_mark(self.logTB.get_insert(), 0.0, True, 0.5, 0.5)

            def myFlush():
                pass

            self.logTB.write = myWrite
            self.logTB.flush = myFlush
            uiLog = logging.StreamHandler(stream=self.logTB)
            uiLog.set_name("ui")
            uiLog.setFormatter(uiLogFormat)
            lg.addHandler(uiLog)
            lg.debug("Gui logging setup.")
            lg.info(f"Using configuration file: {self.config_file.resolve()}")
            if self.config_warn == True:
                lg.warning(f"You're using a fallback configuration file. That's probably not what you want.")

            self.main_win = self.b.get_object("mainWindow")
            self.main_win.set_application(self)

        self.main_win.present()

    def do_command_line(self, command_line):
        lg.debug("Doing command line things")
        options = command_line.get_options_dict()
        # convert GVariantDict -> GVariant -> dict
        options = options.end().unpack()

        if len(options) > 0:
            lg.debug(f"Got command line options: {options}")

        if "config" in options:
            lg.debug(f'Config file given on command line: {str(options["config"])}')
            self.cl_config = pathlib.Path(str(options["config"]))

        self.activate()
        return 0

    def on_about(self, action, param):
        about_dialog = Gtk.AboutDialog(transient_for=self.main_win, modal=True)
        about_dialog.show()

    def do_shutdown(self):
        # remove gui log handler
        for h in lg.handlers:
            if h.get_name() == "ui":
                lg.removeHandler(h)
                lg.debug("Shutting down")
        Gtk.Application.do_shutdown(self)

        # disconnect MQTT
        self._stop_mqtt()

    def on_debug_button(self, button):
        lg.debug("Hello World!")
        self.b.get_object("run_but").set_sensitive(True)
        

    def on_devs_icon_release(self, entry, icon, user_data=None):
        eqe = "eqe" in Gtk.Buildable.get_name(entry)
        self.dev_tree.set_model(self.dev_store[eqe])
        sw = self.dev_tree.get_parent()  # scroll window
        sw.set_min_content_height((self.numSubstrates + 1) * 25)
        if entry.get_icon_name(0) != "emblem-default":
            entry.set_text(self.last_valid_devs[eqe])
        text_is = entry.get_text()
        selection_bitmask = int(text_is, 16)

        bin_mask = bin(selection_bitmask)[2::]
        bin_mask_rev = bin_mask[::-1]

        # iterate through everything and build up the result
        siter = self.dev_store[eqe].get_iter("0")  # substrate iterator
        bit_location = 0
        while siter is not None:
            num_enabled = (
                0  # keeps track of number of enabled devices on this substrate
            )
            diter = self.dev_store[eqe].iter_children(siter)  # device iterator
            while diter is not None:
                if (bit_location + 1 <= len(bin_mask_rev)) and (
                    bin_mask_rev[bit_location] == "1"
                ):
                    self.dev_store[eqe][diter][1] = True
                    num_enabled = num_enabled + 1
                else:
                    self.dev_store[eqe][diter][1] = False
                bit_location = bit_location + 1
                diter = self.dev_store[eqe].iter_next(
                    diter
                )  # advance to the next device
            if num_enabled == 0:
                self.dev_store[eqe][siter][1] = False  # set substrate off
            elif num_enabled == self.numPix:
                self.dev_store[eqe][siter][1] = True  # set substrate on
            else:
                self.dev_store[eqe][siter][2] = True  # set substrate inconsistant
            siter = self.dev_store[eqe].iter_next(
                siter
            )  # advance to the next substrate

        self.po.set_relative_to(entry)
        self.po.show_all()
        # lg.debug(sw.get_allocated_height())
        return True

    def on_pause_button(self, button):
        """Pause experiment operation."""
        lg.info("Pausing run")
        # self.mqttc.publish("gui/pause", "pause", qos=2).wait_for_publish()

    def on_stop_button(self, button):
        """Stop experiment operation."""
        lg.info("Stopping run")
        # self.mqttc.publish("gui/stop", "stop", qos=2).wait_for_publish()

    def on_pd_button(self, button):
        lg.info("Measuring photodiodes")
        # TODO: generate photodiode message

    def on_save_button(self, button):
        """Save current state of widget entries to a file."""
        save_dialog = Gtk.FileChooserNative(
            title="Pick a place to save to",
            transient_for=self.b.get_object("mainWindow"),
            action=Gtk.FileChooserAction.SAVE,
        )
        response = save_dialog.run()
        if response == Gtk.ResponseType.ACCEPT:
            this_file = save_dialog.get_filename()
            lg.info(f"Saving gui state to: {this_file}")

            save_data = {}
            for id_str in self.ids:
                if not id_str.startswith('___'):
                    this_obj = self.b.get_object(id_str)
                    if isinstance(this_obj, gi.repository.Gtk.Switch) or isinstance(this_obj, gi.repository.Gtk.CheckButton) or isinstance(this_obj, gi.overrides.Gtk.ComboBox):
                        save_data[id_str] = {"type": str(type(this_obj)), "value": this_obj.get_active(), "call_to_set": "set_active"}
                    elif isinstance(this_obj, gi.repository.Gtk.SpinButton):
                        save_data[id_str] = {"type": str(type(this_obj)), "value": this_obj.get_value(), "call_to_set": "set_value"}
                    elif isinstance(this_obj, gi.repository.Gtk.Entry):
                        save_data[id_str] = {"type": str(type(this_obj)), "value": this_obj.get_text(), "call_to_set": "set_text"}
                    elif isinstance(this_obj, gi.overrides.Gtk.TreeView):
                        if id_str == "lableTree":
                            save_data[id_str] = {"type": str(type(this_obj)), "value": this_obj.get_model(), "call_to_set": "set_model"}

            with open(save_dialog.get_filename(), "wb") as f:
                pickle.dump(save_data,f)
        else:
            lg.info(f"Save aborted.")

    def on_open_button(self, button):
        """Populate widget entries from data saved in a file."""
        open_dialog = Gtk.FileChooserNative(
            title="Pick a file to load from",
            transient_for=self.b.get_object("mainWindow"),
            action=Gtk.FileChooserAction.OPEN,
        )
        response = open_dialog.run()
        if response == Gtk.ResponseType.ACCEPT:
            this_file = open_dialog.get_filename()
            lg.info(f"Loading gui state from: {this_file}")
            # with open(open_dialog.get_filename(), "r") as f:
            #     data = json.load(f)

            #     for key, value in data:
            #         self.b.get_object(key).set_text(value)
        else:
            lg.info(f"Load aborted.")

    def on_connectivity_button(self, button):
        lg.info("Checking connectivity")
        # TODO: generate connectivity check routine message

    def on_RTD_button(self, button):
        lg.info("Measuring RTD(s)")
        # TODO: generate rtd measurement message

    def on_health_button(self, button):
        lg.info("Checking health")
        # TODO: generate health check message

    def on_home_button(self, button):
        """Home the stage."""
        lg.info("Homing stage")
        self.mqttc.publish("gui/home", "home", qos=2).wait_for_publish()

    def on_stage_read_button(self, button):
        """Read the current stage position."""
        lg.debug("Getting stage position")
        # self.mqttc.publish("gui/read_stage", "read_stage", qos=2).wait_for_publish()

    def on_goto_button(self, button):
        """Goto stage position."""
        lg.debug("Sending the stage some place")
        ax1_pos = self.b.get_object("goto_x").get_text()
        ax2_pos = self.b.get_object("goto_y").get_text()
        payload = json.dumps([ax1_pos, ax2_pos])
        self.mqttc.publish("gui/goto", payload, qos=2).wait_for_publish()

    def on_run_button(self, button):
        """Send run info to experiment orchestrator via MQTT."""
        self.b.get_object("run_but").set_sensitive(False)  # prevent multipress
        run_name = self.b.get_object("run_name").get_text()
        lg.info(f"Starting new run: {run_name}")
        """save_folder = pathlib.Path(self.config["paths"]["save_folder"])
        destination = str(save_folder.joinpath(run_name))

        iv_pixel_address = self.b.get_object("iv_devs").get_text()
        eqe_pixel_address = self.b.get_object("eqe_devs").get_text()
        steadystate_v = 0
        steadystate_i = 0
        v_t = float(self.b.get_object("vocdwell").get_text())
        i_t = float(self.b.get_object("iscdwell").get_text())
        mppt_t = float(self.b.get_object("mpptTime").get_text())
        mppt_params = self.b.get_object("mppt_params").get_text()
        layout_index = "TODO: get this from somewhere, the config file?"  # TODO
        light_recipe = self.b.get_object("light_recipe").get_text()
        light_address = self.config["wavelabs"]["address"]
        motion_address = self.config["motion"]["address"]
        scan_points = float(self.b.get_object("sweepSteps").get_text())
        scan_nplc = float(self.b.get_object("nplc").get_text())
        steadystate_nplc = scan_nplc
        scan_step_delay = float(self.b.get_object("sweepDelay").get_text())
        sm_terminator = self.config["smu"]["terminator"]
        sm_baud = int(self.config["smu"]["baud"])
        sm_address = self.config["smu"]["address"]
        pcb_address = motion_address
        ignore_diodes = True
        lia_address = self.config["lia"]["address"]
        mono_address = self.config["monochromator"]["address"]
        psu_address = self.config["psu"]["address"]
        psu_vs = [
            float(self.config["psu"]["ch1_voltage"]),
            float(self.config["psu"]["ch2_voltage"]),
            float(self.config["psu"]["ch3_voltage"]),
        ]
        psu_is = [
            float(self.b.get_object("gblc").get_text()),
            float(self.b.get_object("rblc").get_text()),
            0,
        ]
        eqe_smu_v = float(self.b.get_object("eqedevbias").get_text())
        eqe_ref_meas_path = "What's this?"  # TODO
        eqe_ref_cal_path = self.config["paths"]["eqe_ref_cal_path"]
        eqe_ref_spec_path = self.config["paths"]["eqe_ref_spec_path"]
        eqe_start_wl = float(self.b.get_object("nmStart").get_text())
        eqe_end_wl = float(self.b.get_object("nmStop").get_text())
        eqe_step = float(self.b.get_object("nmStep").get_text())
        eqe_num_wls = int(np.absolute(eqe_end_wl - eqe_start_wl) / eqe_step) + 1
        eqe_integration_time = self.b.get_object("eqe_int").get_text()
        eqe_grating_change_wls = self.config["monochromator"]["grating_change_wls"]
        eqe_grating_change_wls = [int(x) for x in eqe_grating_change_wls.split(",")]
        eqe_filter_change_wls = self.config["monochromator"]["filter_change_wls"]
        eqe_filter_change_wls = [int(x) for x in eqe_filter_change_wls.split(",")]

        # make settings dict
        # TODO: this looks a bit fragile. I wonder if we can automate it...
        settings = {
            "destination": destination,
            "operator": "",
            "run_description": "",
            "experimental_parameter": "",
            "iv_pixel_address": iv_pixel_address,
            "eqe_pixel_address": eqe_pixel_address,
            "mqtt_host": self.MQTTHOST,
            "steadystate_v": steadystate_v,
            "steadystate_i": steadystate_i,
            "v_t": v_t,
            "i_t": i_t,
            "mppt_t": mppt_t,
            "mppt_params": mppt_params,
            "light_recipe": light_recipe,
            "light_address": light_address,
            "motion_address": motion_address,
            "scan_points": scan_points,
            "scan_nplc": scan_nplc,
            "steadystate_nplc": steadystate_nplc,
            "scan_step_delay": scan_step_delay,
            "sm_terminator": sm_terminator,
            "sm_baud": sm_baud,
            "sm_address": sm_address,
            "pcb_address": pcb_address,
            "ignore_diodes": ignore_diodes,
            "lia_address": lia_address,
            "mono_address": mono_address,
            "psu_address": psu_address,
            "psu_vs": psu_vs,
            "psu_is": psu_is,
            "eqe_smu_v": eqe_smu_v,
            "eqe_ref_meas_path": eqe_ref_meas_path,
            "eqe_ref_cal_path": eqe_ref_cal_path,
            "eqe_ref_spec_path": eqe_ref_spec_path,
            "eqe_start_wl": eqe_start_wl,
            "eqe_end_wl": eqe_end_wl,
            "eqe_num_wls": eqe_num_wls,
            "eqe_integration_time": eqe_integration_time,
            "eqe_grating_change_wls": eqe_grating_change_wls,
            # "eqe_grating_change_wls": eqe_filter_change_wls,
        }

        # add optional parameters if required
        if not self.b.get_object("autoiv").get_active():
            scan_start_override_1 = self.b.get_object("sweep1_start").get_value()
            scan_end_override_1 = self.b.get_object("sweep1_end").get_value()
            scan_start_override_2 = self.b.get_object("sweep2_start").get_value()
            scan_end_override_2 = self.b.get_object("sweep2_end").get_value()
            settings["scan_start_override_1"] = scan_start_override_1
            settings["scan_end_override_1"] = scan_end_override_1
            settings["scan_start_override_2"] = scan_start_override_2
            settings["scan_end_override_2"] = scan_end_override_2

        # send settings dict over mqtt
        payload = json.dumps(
            settings
        )  # TODO: could also use pickle here, which might be more general
        lg.debug("Run Payload:")
        lg.debug(payload)
        self.mqttc.publish(
            "gui", payload, qos=2
        ).wait_for_publish()  # TODO: probably we don't wait for this """

    def on_cal_eqe_button(self, button):
        """Measure EQE calibration photodiode."""
        save_folder = pathlib.Path(self.config["paths"]["save_folder"])
        run_name = self.b.get_object("run_name").get_text()
        destination = str(save_folder.joinpath(run_name))

        # Arbitrary dummy bitmask containing a single pixel.
        bitmask_value = 1
        eqe_pixel_address = f"0x{bitmask_value:{self.def_fmt_str}}"

        motion_address = self.config["motion"]["address"]
        sm_terminator = self.config["smu"]["terminator"]
        sm_baud = int(self.config["smu"]["baud"])
        sm_address = self.config["smu"]["address"]
        pcb_address = motion_address
        ignore_diodes = True
        lia_address = self.config["lia"]["address"]
        mono_address = self.config["monochromator"]["address"]
        psu_address = self.config["psu"]["address"]
        psu_vs = [
            float(self.config["psu"]["ch1_voltage"]),
            float(self.config["psu"]["ch2_voltage"]),
            float(self.config["psu"]["ch3_voltage"]),
        ]
        psu_is = [
            float(self.b.get_object("gblc").get_text()),
            float(self.b.get_object("rblc").get_text()),
            0,
        ]
        eqe_smu_v = float(self.b.get_object("eqedevbias").get_text())
        eqe_ref_meas_path = "TODO: what's this?"  # TODO
        eqe_ref_cal_path = self.config["paths"]["eqe_ref_cal_path"]
        eqe_ref_spec_path = self.config["paths"]["eqe_ref_spec_path"]
        eqe_start_wl = float(self.b.get_object("nmStart").get_text())
        eqe_end_wl = float(self.b.get_object("nmStop").get_text())
        eqe_step = float(self.b.get_object("nmStep").get_text())
        eqe_num_wls = int(np.absolute(eqe_end_wl - eqe_start_wl) / eqe_step) + 1
        eqe_integration_time = self.b.get_object("eqe_int").get_text()
        eqe_grating_change_wls = self.config["monochromator"]["grating_change_wls"]
        eqe_grating_change_wls = [int(x) for x in eqe_grating_change_wls.split(",")]
        eqe_filter_change_wls = self.config["monochromator"]["filter_change_wls"]
        eqe_filter_change_wls = [int(x) for x in eqe_filter_change_wls.split(",")]

        # TODO: this looks a bit fragile. I wonder if we can automate it...
        settings = {
            "destination": destination,
            "operator": "",
            "run_description": "",
            "experimental_parameter": "",
            "mqtt_host": self.MQTTHOST,
            "eqe_pixel_address": eqe_pixel_address,
            "calibrate_eqe": True,
            "position_override": self.config["stage"]["photodiode_offset"],
            "motion_address": motion_address,
            "sm_terminator": sm_terminator,
            "sm_baud": sm_baud,
            "sm_address": sm_address,
            "pcb_address": pcb_address,
            "ignore_diodes": ignore_diodes,
            "lia_address": lia_address,
            "mono_address": mono_address,
            "psu_address": psu_address,
            "psu_vs": psu_vs,
            "psu_is": psu_is,
            "eqe_smu_v": eqe_smu_v,
            "eqe_ref_meas_path": eqe_ref_meas_path,
            "eqe_ref_cal_path": eqe_ref_cal_path,
            "eqe_ref_spec_path": eqe_ref_spec_path,
            "eqe_start_wl": eqe_start_wl,
            "eqe_end_wl": eqe_end_wl,
            "eqe_num_wls": eqe_num_wls,
            "eqe_integration_time": eqe_integration_time,
            "eqe_grating_change_wls": eqe_grating_change_wls,
            # "eqe_grating_change_wls": eqe_filter_change_wls,
        }

        # send settings dict over mqtt
        payload = json.dumps(
            settings
        )  # TODO: could also use pickle here, which might be more general
        self.mqttc.publish(
            "gui", payload, qos=2
        ).wait_for_publish()  # TODO: probably we don't wait for this

    def on_cal_ch3_button(self, button):
        self.calibrate_psu(3)

    def on_cal_ch2_button(self, button):
        self.calibrate_psu(2)

    def on_cal_ch1_button(self, button):
        self.calibrate_psu(1)

    def on_smart_mode_activate(self, button):
        self.update_gui()

    def update_gui(self, *args):
        # lg.debug("Updating gui...")
        if self.b.get_object("ad_switch").get_active():
            self.b.get_object("sd_lab").set_sensitive(False)
            self.b.get_object("source_delay").set_sensitive(False)
            self.b.get_object("source_delay").set_visibility(False)
            self.b.get_object("sd_dt").set_sensitive(False)
        else:
            self.b.get_object("sd_lab").set_sensitive(True)
            self.b.get_object("source_delay").set_sensitive(True)
            self.b.get_object("source_delay").set_visibility(True)
            self.b.get_object("sd_dt").set_sensitive(True)

        if self.b.get_object("return_switch").get_active():
            self.b.get_object("sweep_check").set_label("Step 2: I-V Sweeps")
        else:
            self.b.get_object("sweep_check").set_label("Step 2: I-V Sweep")

        me = self.b.get_object("i_dwell_check")
        parent = me.get_parent()
        if me.get_active():
            for sib in parent.get_children():
                sib.set_sensitive(True)
        else:
            for sib in parent.get_children():
                sib.set_sensitive(False)
            me.set_sensitive(True)

        me = self.b.get_object("sweep_check")
        parent = me.get_parent()
        if me.get_active():
            for sib in parent.get_children():
                sib.set_sensitive(True)
        else:
            for sib in parent.get_children():
                sib.set_sensitive(False)
            me.set_sensitive(True)

        me = self.b.get_object("v_dwell_check")
        parent = me.get_parent()
        if me.get_active():
            for sib in parent.get_children():
                sib.set_sensitive(True)
        else:
            for sib in parent.get_children():
                sib.set_sensitive(False)
            me.set_sensitive(True)

        me = self.b.get_object("mppt_check")
        parent = me.get_parent()
        if me.get_active():
            for sib in parent.get_children():
                sib.set_sensitive(True)
        else:
            for sib in parent.get_children():
                sib.set_sensitive(False)
            me.set_sensitive(True)

    # def on_ad_switch_state_set(self, switch, state):
    #    self.update_gui()

    # def on_return_switch_state_set(self, switch, state):
    #    self.update_gui()

    def calibrate_psu(self, channel):
        """Measure psu calibration photodiode."""
        save_folder = pathlib.Path(self.config["paths"]["save_folder"])
        run_name = self.b.get_object("run_name").get_text()
        destination = str(save_folder.joinpath(run_name))

        motion_address = self.config["motion"]["address"]
        sm_terminator = self.config["smu"]["terminator"]
        sm_baud = int(self.config["smu"]["baud"])
        sm_address = self.config["smu"]["address"]
        pcb_address = motion_address
        ignore_diodes = True
        psu_address = self.config["psu"]["address"]

        # TODO: this looks a bit fragile. I wonder if we can automate it...
        settings = {
            "destination": destination,
            "operator": "",
            "run_description": "",
            "experimental_parameter": "",
            "mqtt_host": self.MQTTHOST,
            "position_override": self.config["stage"]["photodiode_offset"],
            "motion_address": motion_address,
            "sm_terminator": sm_terminator,
            "sm_baud": sm_baud,
            "sm_address": sm_address,
            "pcb_address": pcb_address,
            "ignore_diodes": ignore_diodes,
            "psu_address": psu_address,
            "calibrate_psu": True,
            "calibrate_psu_ch": channel,
        }

        # send settings dict over mqtt
        payload = json.dumps(
            settings
        )  # TODO: could also use pickle here, which might be more general
        self.mqttc.publish(
            "gui", payload, qos=2
        ).wait_for_publish()  # TODO: probably we don't wait for this

    # TODO: I think this function isn't needed if we pickle the objects we send over MQTT
    # def _get_layouts_str(self):
    #     """Read and format layouts from config file.

    #     This function strips the comments from layout sections of the config file so
    #     they can be sent more efficiently over MQTT.
    #     """
    #     layout_names = self.config["substrates"]["layout_names"].split(",")

    #     layouts_str = f""
    #     for name in layout_names:
    #         layouts_str += f"""
    #         [{name}]
    #         positions={self.config[name]["positions"]}
    #         areas={self.config[name]["areas"]}
    #         """

    #     return layouts_str


if __name__ == "__main__":
    app = App()
    app.run(sys.argv)
