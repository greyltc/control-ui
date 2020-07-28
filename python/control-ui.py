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


class App(Gtk.Application):

    def __init__(self, *args, **kwargs):
        """Constructor."""
        super().__init__(
            *args,
            application_id="net.christoforo.control-ui",
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
            **kwargs,
        )
        self.main_win = None
        self.mqtt_setup = False
        self.mqtt_connecting = False
        # to keep track of the two toggle buttons in the utility panel
        self.all_mux_switches_open = True
        self.in_iv_mode = True

        # allow configuration file location to be specified by command line argument
        self.add_main_option(
            "config",
            ord("c"),
            GLib.OptionFlags.NONE,
            GLib.OptionArg.FILENAME,
            "Configuration file",
            None,
        )

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
            cs = [str(x+1) for x in cs]
        except IndexError:
            # if number of columns not given, must be 0
            cs = ['']

        subdes = [f"{n}{m}" for m in rs for n in cs]

        return subdes

    def _start_mqtt(self):
        """Start the MQTT client and subscribe to the CLI topic."""
        self.mqtt_connecting = True

        def on_message(mqttc, obj, msg):
            """Act on an MQTT message."""
            try:
                msg = pickle.loads(msg.payload)
                #m = json.loads(msg.payload)
                #lg.debug(f"New message: {msg}")
            except:
                msg = None

            if msg is not None:
                if 'log' in msg: # log update message
                    lg.log(msg['log']['level'], msg['log']['text'])
                if 'pos' in msg: # position update message
                    pos = msg['pos']
                    if len(pos) != self.num_axes:
                        lg.warning(f"Stage dimension mismatch")
                    else:
                        for i,val in enumerate(pos):
                            try:
                                self.gotos[i].set_value(val)
                            except:
                                 self.gotos[i].set_text('')
                                 lg.warning(f"Failed to read axis {i+1} position")


        try:
            # connect to mqtt broker
            self.MQTTHOST = self.config["network"]["MQTTHOST"]
            self.mqttc = mqtt.Client()
            self.mqttc.on_message = on_message
            self.mqttc.connect(self.MQTTHOST)
            # subscribe to cli topic to report back on progress
            #self.mqttc.subscribe("cli/#", qos=2)

            # a channel for progress messages
            self.mqttc.subscribe("status/#", qos=2)

            # a channel for results from completed commands
            self.mqttc.subscribe("response/#", qos=2)  
            self.mqttc.loop_start()
            self.mqtt_setup = True
        except:
            lg.error("Unable to connect to the backend.")
            self.mqtt_setup = False
        self.mqtt_connecting = False

    def _stop_mqtt(self):
        """Stop the MQTT client."""
        self.mqttc.loop_stop()
        self.mqttc.disconnect()

    def do_startup(self):
        lg.debug(f"Starting up app from {__file__}")
        Gtk.Application.do_startup(self)
        self.this_file = pathlib.Path(__file__)

        galde_ui_xml_file_name = "ui.glade"
        gui_file = pathlib.Path(galde_ui_xml_file_name)
        python_gui_file = ("python" / gui_file)
        file_sibling_gui_file = self.this_file.parent / galde_ui_xml_file_name
        if gui_file.is_file():
            self.galde_ui_xml_file = str(gui_file.resolve())
        elif python_gui_file.is_file():  # so that debug from code works
            self.galde_ui_xml_file = str(python_gui_file.resolve())
        elif file_sibling_gui_file.is_file():  # look in __file__'s dir
            self.galde_ui_xml_file = str(file_sibling_gui_file.resolve())
        else:
            raise (ValueError("Can't find glade file!"))

        self.b = Gtk.Builder()
        self.b.add_from_file(self.galde_ui_xml_file)

        # crawl the builder object and mine all the object ID strings we can
        # this list of id strings will need to be filtered by the load and save functions
        # a good starting point will be to discard those starting with "___"
        # I should probably just rename all the saveable/loadable stuff to contain
        # some special character so the filtering becomes easier
        self.ids = []
        for o in self.b.get_objects():
            try:
                self.ids.append(Gtk.Buildable.get_name(o))
            except:
                pass

    def setup_dev_stores(self, num_substrates, num_pix, dev_stores, label_store):
        for store in dev_stores:
            checked = True
            inconsistent = False
            # fill in the model
            for i in range(num_substrates):
                if label_store[i][0] == "":
                    label = label_store[i][1]
                else:
                    label = label_store[i][0]
                piter = store.append(None, [label, checked, inconsistent])
                j = 1
                while j <= num_pix:
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
            if num_selected == self.num_pix:
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


    def setup_label_tree(self, labels, substrate_designators, cell_y_padding):
        labelTree = self.b.get_object("label_tree")
        labelStore = Gtk.ListStore(str, str, int)

        for i in range(self.num_substrates):
            labelStore.append([labels[i], substrate_designators[i], cell_y_padding])
        labelTree.set_model(labelStore)

        # the editable substrate label col
        renderEdit = Gtk.CellRendererText()
        renderEdit.set_property("editable", True)
        labels = Gtk.TreeViewColumn(
            "Substrate Label", renderEdit, text=0, placeholder_text=1, ypad=2
        )
        # only append the col if it's not already there
        # fixes double col on file load
        if labelTree.get_columns() == []:
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
        if text == "": # if it's empty use the default
            self.dev_store[0][path][0] = self.label_store[path][1]
            self.dev_store[1][path][0] = self.label_store[path][1]
        else: # otherwise use the user's one
            self.label_shadow[int(path)] = text
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
        if self.mqtt_setup == True:
            if self.mqttc.is_connected():
                status = "Connected"
                self.mqtt_connected = True
            else:
                status = "Disconnected"
                self.mqtt_connected = False
            self.b.get_object("headerBar").set_subtitle(f"Status: {status}")
        else:
            self.mqtt_connected = False
            status = "Disconnected"
            self.b.get_object("headerBar").set_subtitle(f"Status: {status}")
            if self.mqtt_connecting == False: # don't spam connections
                self._start_mqtt()
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
            self.log_win_adj = self.b.get_object("vert_log_win_scroll_adj")


            def myWrite(buf):
                # the log update should not be done on the main gui thread
                # or else segfault badness
                GLib.idle_add(self.append_to_log_window, str(buf))


            def myFlush():
                pass

            self.logTB.write = myWrite
            self.logTB.flush = myFlush
            uiLog = logging.StreamHandler(stream=self.logTB)
            uiLog.setLevel(logging.INFO)
            uiLog.set_name("ui")
            uiLog.setFormatter(uiLogFormat)
            lg.addHandler(uiLog)
            lg.debug("Gui logging setup.")

            self.config = configparser.ConfigParser(
                interpolation=configparser.ExtendedInterpolation()
            )
            
            example_config_file_name = "example_config.ini"
            config_file_name = "measurement_config.ini"
            self.config_file = pathlib.Path(config_file_name)

            # let's figure out where the configuration file is
            config_env_var = "MEASUREMENT_CONFIGURATION_FILE_NAME"
            if config_env_var in os.environ:
                env_config = pathlib.Path(os.environ.get(config_env_var))
            else:
                env_config = pathlib.Path()
            home_config = pathlib.Path.home() / config_file_name
            local_config = pathlib.Path(config_file_name)
            example_config = pathlib.Path(example_config_file_name)
            example_python_config = pathlib.Path("python") / example_config_file_name
            file_sibling_config = self.this_file.parent / example_config_file_name
            self.config_warn = False
            if self.cl_config.is_file():  # priority 1: check the command line
                lg.debug("Using config file from command line")
                self.config_file = self.cl_config
            elif env_config.is_file():  # priority 2: check the environment
                lg.debug(f"Using config file from {config_env_var} variable")
                self.config_file = env_config
            elif local_config.is_file():  # priority 3: check in the current drectory
                #lg.debug(f"Using local config file {local_config.resolve()}")
                self.config_file = local_config
            elif home_config.is_file():  # priority 4: check in home dir
                lg.debug(
                    f"Using config file {config_file_name} in home dir: {pathlib.Path.home()}"
                )
                self.config_file = home_config
            elif example_config.is_file():  # priority 5: check in cwd for example
                lg.debug(f"Using example config file: {example_config.resolve()}")
                self.config_file = example_config
                self.config_warn = True
            elif example_python_config.is_file():  # priority 6: check in python/for example
                lg.debug(f"Using example config file: {example_python_config.resolve()}")
                self.config_file = example_python_config
                self.config_warn = True
            elif file_sibling_config.is_file():
                lg.debug(f"Using example config file next to __file__: {file_sibling_config.resolve()}")
                self.config_file = file_sibling_config
                self.config_warn = True
            else:  # and give up
                lg.error("Unable to find a configuration file to load.")
                raise (ValueError("No config file"))

            lg.info(f"Using configuration file: {self.config_file.resolve()}")
            if self.config_warn == True:
                lg.warning(f"Running with the example configuration file.")

            try:
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
            self.num_substrates = len(self.substrate_designators)

            self.num_pix = len(
                self.config[self.config["substrates"]["active_layout"]]["pixels"].split(",")
            )
            self.live_data_uri = self.config["network"]["live_data_uri"]

            # stage specific stuff
            esl = self.config["stage"]["uri"].split('://')[1].split('/')[0]
            if ',' in esl:
                esl = [float(x) for x in esl.split(',')]
            else:
                esl = [float(esl)]
            length_oom = max([math.ceil(math.log10(x)) for x in esl])
            steps_per_mm = int(self.config["stage"]["uri"].split('://')[1].split('/')[1])
            movement_res = 1/steps_per_mm
            movement_res_oom = abs(math.floor(math.log10(movement_res)))
            goto_field_width = length_oom + 1 + movement_res_oom

            self.gotos = [self.b.get_object("goto_x"), self.b.get_object("goto_y"), self.b.get_object("goto_z")]
            adjusters = [self.b.get_object("stage_x_adj"), self.b.get_object("stage_y_adj"), self.b.get_object("stage_z_adj")]
            end_buffer_in_mm = 5 # don't allow the user to go less than this from the ends
            for i, axlen in enumerate(esl):
                self.gotos[i].set_width_chars(goto_field_width)
                self.gotos[i].set_digits(movement_res_oom)
                adjusters[i].set_value(axlen/2)
                adjusters[i].set_lower(end_buffer_in_mm)
                adjusters[i].set_upper(axlen-end_buffer_in_mm)

            # hide unused axes
            self.num_axes = len(esl)
            if self.num_axes < 3:
                o = self.b.get_object("gtzl")
                o.set_visible(False)
                o = self.b.get_object("goto_z")
                o.set_visible(False)
            if self.num_axes < 2:
                o = self.b.get_object("gtyl")
                o.set_visible(False)
                o = self.b.get_object("goto_y")
                o.set_visible(False)
            
            # handle custom locations
            location_names = [conf for conf in self.config["stage"]]
            location_names.remove('uri')
            pl = self.b.get_object("places_list")
            self.custom_coords = []
            for name in location_names:
                coord = self.config["stage"][name].split(',')
                coord = [float(v) for v in coord]
                self.custom_coords.append(coord)
                pl.append([name])

            # list that shadows the device label names
            self.label_shadow = ['']*self.num_substrates

            self.label_tree, self.label_store = self.setup_label_tree(self.label_shadow, self.substrate_designators, 0)

            self.dev_store = [
                Gtk.TreeStore(str, bool, bool),
                Gtk.TreeStore(str, bool, bool),
            ]  # [iv devs, eqe devs]
            self.setup_dev_stores(self.num_substrates, self.num_pix, self.dev_store, self.label_store)

            self.dev_tree = self.b.get_object("devTV")
            self.setup_dev_tree(self.dev_tree)

            # TODO: do this in a non-obsolete way (probably w/ css)
            fontdesc = Pango.FontDescription("monospace")

            max_devices = self.num_pix * self.num_substrates
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

            self.approx_seconds_per_iv = 50
            self.approx_seconds_per_eqe = 150

            wv = self.b.get_object("wv")
            if self.live_data_uri != "":
                wv.load_uri(self.live_data_uri)

            # start MQTT client
            self._start_mqtt()

            # send config file to CLI MQTT client
            # not really sure why this should go out now...
            # with open(self.config_file, "r") as f:
            #     payload = json.dumps(f.read())
            # self.mqttc.publish("gui/config", payload, qos=2)

            self.tick()
            self.ticker_id = GLib.timeout_add_seconds(1, self.tick, None)
            self.b.connect_signals(self)  # maps all ui callbacks to functions here


            self.main_win = self.b.get_object("mainWindow")
            self.main_win.set_application(self)

        self.main_win.present()


    # gets called when the user selects a custom position
    def on_load_pos(self, cb):
        j = cb.get_active()
        pos = self.custom_coords[j]
        for i,coord in enumerate(pos):
            self.gotos[i].set_value(coord)


    def do_command_line(self, command_line):
        lg.debug("Doing command line things")
        options = command_line.get_options_dict()
        # convert GVariantDict -> GVariant -> dict
        options = options.end().unpack()

        # something that fails the is_file test later
        self.cl_config = pathlib.Path() 

        if len(options) > 0:
            lg.debug(f"Got command line options: {options}")

        if "config" in options:
            conf = bytes(options["config"]).decode().rstrip('\0')
            lg.debug(f'Config file given on command line: {conf}')
            self.cl_config = pathlib.Path(conf)

        self.activate()
        return 0


    # adds text, then scrolls the log window to the bottom
    # (called from GLib.idle_add or else segfault!)
    def append_to_log_window(self, text):
        ei = self.logTB.get_end_iter()
        self.logTB.insert(ei, text)
        adj = self.log_win_adj
        adj.set_value(adj.get_upper())


    def on_about(self, action, param):
        about_dialog = Gtk.AboutDialog(transient_for=self.main_win, modal=True)
        about_dialog.show()


    def do_shutdown(self):
        # stop the ticker
        GLib.source_remove(self.ticker_id)

        # disconnect MQTT
        self._stop_mqtt()

        # remove gui log handler
        for h in lg.handlers:
            if h.get_name() == "ui":
                lg.removeHandler(h)
                lg.debug("Shutting down")

        Gtk.Application.do_shutdown(self)


    def on_debug_button(self, button):
        lg.debug("Hello World!")
        self.b.get_object("run_but").set_sensitive(True)


    def on_devs_icon_release(self, entry, icon, user_data=None):
        eqe = "eqe" in Gtk.Buildable.get_name(entry)
        self.dev_tree.set_model(self.dev_store[eqe])
        sw = self.dev_tree.get_parent()  # scroll window
        sw.set_min_content_height((self.num_substrates + 1) * 25)
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
            num_enabled = 0  # keeps track of number of enabled devices on this substrate
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
            elif num_enabled == self.num_pix:
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
        # TODO: consider implimenting this
        # self.mqttc.publish("gui/pause", "pause", qos=2).wait_for_publish()


    def on_stop_button(self, button):
        """Stop experiment operation."""
        lg.info("Stopping run")
        # TODO not sure where to send this
        self.mqttc.publish("gui/stop", "stop", qos=2).wait_for_publish()


    def harvest_gui_data(self):
        """
        Packages up all the (relevant) info the user has entered into the gui
        Used when the user saves the gui state or clicks the button to start a measurement run
        """
        gui_data = {}
        for id_str in self.ids:
            if not id_str.startswith('___'):  # ignore ids that don't have their value explicitly set
                this_obj = self.b.get_object(id_str)
                if isinstance(this_obj, gi.repository.Gtk.Switch) or isinstance(this_obj, gi.repository.Gtk.CheckButton) or isinstance(this_obj, gi.overrides.Gtk.ComboBox):
                    gui_data[id_str] = {"type": str(type(this_obj)), "value": this_obj.get_active(), "call_to_set": "set_active"}
                elif isinstance(this_obj, gi.repository.Gtk.SpinButton):
                    gui_data[id_str] = {"type": str(type(this_obj)), "value": this_obj.get_value(), "call_to_set": "set_value"}
                elif isinstance(this_obj, gi.repository.Gtk.Entry):
                    gui_data[id_str] = {"type": str(type(this_obj)), "value": this_obj.get_text(), "call_to_set": "set_text"}
                elif isinstance(this_obj, gi.overrides.Gtk.TreeView):  # the TreeViews are unfortunately not pickalble
                    if id_str == "label_tree":
                        gui_data[id_str] = {"type": str(type(this_obj)), "value": self.label_shadow, "call_to_set": None}
        return gui_data


    def on_save_button(self, button):
        """Save current state of widget entries to a file."""
        save_dialog = Gtk.FileChooserNative(
            title="Pick a place to save to",
            transient_for=self.b.get_object("mainWindow"),
            action=Gtk.FileChooserAction.SAVE,
        )
        filt = Gtk.FileFilter()
        filt.add_pattern("*.dat")
        filt.set_name("GUI State Files (*.dat)")
        save_dialog.add_filter(filt)

        filt = Gtk.FileFilter()
        filt.add_pattern("*")
        filt.set_name("All Files")
        save_dialog.add_filter(filt)

        save_dialog.set_current_name("gui_state.dat")

        response = save_dialog.run()
        if response == Gtk.ResponseType.ACCEPT:
            this_file = save_dialog.get_filename()
            lg.info(f"Saving gui state to: {this_file}")

            save_data = self.harvest_gui_data()
            with open(this_file, "wb") as f:
                pickle.dump(save_data,f,protocol=pickle.HIGHEST_PROTOCOL)
        else:
            lg.info(f"Save aborted.")


    def on_open_button(self, button):
        """Populate widget entries from data saved in a file."""
        open_dialog = Gtk.FileChooserNative(
            title="Pick a file to load from",
            transient_for=self.b.get_object("mainWindow"),
            action=Gtk.FileChooserAction.OPEN,
        )

        filt = Gtk.FileFilter()
        filt.add_pattern("*.dat")
        filt.set_name("GUI State Files (*.dat)")
        open_dialog.add_filter(filt)

        filt = Gtk.FileFilter()
        filt.add_pattern("*")
        filt.set_name("All Files")
        open_dialog.add_filter(filt)

        response = open_dialog.run()
        if response == Gtk.ResponseType.ACCEPT:
            this_file = open_dialog.get_filename()
            lg.info(f"Loading gui state from: {this_file}")
            with open(this_file, "rb") as f:
                load_data = pickle.load(f)

            for id_str, obj_info in load_data.items():
                if id_str == 'label_tree':  # load and apply the device labels
                    # NOTE: loading will likely crash if loading into a setup with the wrong number of pixels/devices
                    try:
                        self.label_shadow = obj_info['value']
                        # i'm going to assume the substrate designators didn't change across loads...
                        self.label_tree, self.label_store = self.setup_label_tree(self.label_shadow, self.substrate_designators, 0)
                        for i, lab in enumerate(self.label_shadow):
                            self.store_substrate_label(None, str(i), lab)
                    except:
                        lg.info(f"Loading substrate labels failed.")
                else:
                    this_obj = self.b.get_object(id_str)
                    call_to_set = getattr(this_obj, obj_info['call_to_set'])
                    call_to_set(obj_info['value'])
            self.update_gui()
        else:
            lg.info(f"Load aborted.")


    def on_connectivity_button(self, button):
        lg.info("Connectivity check started")
        iv_dev_txt = self.b.get_object("iv_devs").get_text()
        iv_dev_num = int(iv_dev_txt, 16)
        eqe_dev_txt = self.b.get_object("eqe_devs").get_text()
        eqe_dev_num = int(eqe_dev_txt, 16)
        any_dev_num = iv_dev_num|eqe_dev_num # combine the selections for the connectivity check
        msg = {'cmd':'round_robin',
        'type': 'connectivity',
        'devices': self.bitmask_to_some_lists(hex(any_dev_num))['selections'],
        'pcb': self.config['controller']['address'],
        'smu_address': self.config['smu']['address'],
        'smu_baud': int(self.config['smu']['baud']),
        }
        pic_msg = pickle.dumps(msg, protocol=pickle.HIGHEST_PROTOCOL)
        self.mqttc.publish("cmd/uitl", pic_msg, qos=2).wait_for_publish()


    def on_RTD_button(self, button):
        lg.info("RTD temperature measurement started")
        msg = {'cmd':'round_robin',
        'type': 'rtd',
        'devices': self.bitmask_to_some_lists(self.b.get_object("iv_devs").get_text())['selections'],
        'pcb': self.config['controller']['address'],
        'smu_address': self.config['smu']['address'],
        'smu_baud': int(self.config['smu']['baud']),
        }
        pic_msg = pickle.dumps(msg, protocol=pickle.HIGHEST_PROTOCOL)
        self.mqttc.publish("cmd/uitl", pic_msg, qos=2).wait_for_publish()


    def on_current_button(self, button):
        lg.info("Current measurement started")
        msg = {'cmd':'round_robin',
        'type': 'current',
        'devices': self.bitmask_to_some_lists(self.b.get_object("iv_devs").get_text())['selections'],
        'pcb': self.config['controller']['address'],
        'smu_address': self.config['smu']['address'],
        'smu_baud': int(self.config['smu']['baud']),
        }
        pic_msg = pickle.dumps(msg, protocol=pickle.HIGHEST_PROTOCOL)
        self.mqttc.publish("cmd/uitl", pic_msg, qos=2).wait_for_publish()


    def on_device_toggle(self, button):
        """
        allows the user to connect anyone pixel, or disconnect them all
        toggels between connecting the first selected IV device and selecting nothing
        """
        if self.all_mux_switches_open == True:
            self.all_mux_switches_open = False
            some_lists = self.bitmask_to_some_lists(self.b.get_object("iv_devs").get_text(),maximum=1)
            if len(some_lists['dev_nums']) == 0:
                lg.info("No devices selected for connection")
            else:
                user_label = some_lists['user_labels'][0]
                if user_label == '':
                    user_label = some_lists['subs_names'][0]
                lg.info(f"Connecting device: {user_label}-{some_lists['sub_dev_nums'][0]}")
                msg = {'cmd':'for_pcb', 'pcb_cmd':some_lists['selections'][0], 'pcb':self.config['controller']['address']}
                pic_msg = pickle.dumps(msg, protocol=pickle.HIGHEST_PROTOCOL)
                self.mqttc.publish("cmd/uitl", pic_msg, qos=2).wait_for_publish()
        else:
            self.all_mux_switches_open = True
            lg.info("Disconnecting all devices")
            msg = {'cmd':'for_pcb', 'pcb_cmd':'s', 'pcb':self.config['controller']['address']}
            pic_msg = pickle.dumps(msg, protocol=pickle.HIGHEST_PROTOCOL)
            self.mqttc.publish("cmd/uitl", pic_msg, qos=2).wait_for_publish()


    def bitmask_to_some_lists(self, bitmask, maximum=float("inf")):
        """
        input: takes a hex bitmask device selection string, and a maximum number of devices to find, starting with the LSB
        outputs a dict consisting of:
        list of selected device numbers
        list of substrate numbers those are on
        list of device numbers relative to that substrate
        list of substrate designators the devices are on
        list of substrate labels the devices are on
        list of device selection strings for the mux
        """
        selection_bitmask = int(bitmask, 16)
        bin_mask = bin(selection_bitmask)[2::]
        bin_mask_rev = bin_mask[::-1]
        dev_nums = []
        subs_nums = []
        sub_dev_nums = []
        subs_names = []
        user_labels = []
        selections = []
        for i,c in enumerate(bin_mask_rev):
            if c == '1':
                dev_num = i
                dev_nums += [dev_num]
                subs_num = math.floor(i/self.num_pix)
                subs_nums += [subs_num]
                sub_dev_num = dev_num%self.num_pix + 1  # we'll count these from 1 here
                sub_dev_nums += [sub_dev_num]
                subs_name = self.substrate_designators[subs_num]
                subs_names += [subs_name]
                user_label = self.label_shadow[subs_num]
                user_labels += [user_label]
                selection = f"s{subs_name}{sub_dev_num}".lower()
                selections += [selection]
                if len(dev_nums) >= maximum:
                    break
        return({'dev_nums':dev_nums, 'subs_nums':subs_nums, 'sub_dev_nums':sub_dev_nums, 'subs_names':subs_names, 'user_labels':user_labels, 'selections':selections})


    def on_mode_toggle_button(self, button):
        """
        toggles the EQE/IV realys in the control box
        """
        if (self.in_iv_mode == True):
            self.in_iv_mode = False
            pcb_cmd = 'iv'
            notice = "Entering I-V mode"
        else:
            self.in_iv_mode = True
            pcb_cmd = 'eqe'
            notice = "Entering EQE mode"
        lg.info(notice)
        msg = {'cmd':'for_pcb', 'pcb_cmd':pcb_cmd, 'pcb':self.config['controller']['address']}
        pic_msg = pickle.dumps(msg, protocol=pickle.HIGHEST_PROTOCOL)
        self.mqttc.publish("cmd/uitl", pic_msg, qos=2).wait_for_publish()


    def on_health_button(self, button):
        lg.info("HEALTH CHECK INITIATED")
        msg = {'cmd':'check_health',
        'pcb': self.config['controller']['address'],
        'psu': self.config['psu']['address'],
        'smu_address': self.config['smu']['address'],
        'smu_baud': int(self.config['smu']['baud']),
        }
        pic_msg = pickle.dumps(msg, protocol=pickle.HIGHEST_PROTOCOL)
        self.mqttc.publish("cmd/uitl", pic_msg, qos=2).wait_for_publish()
    
    def move_warning(self):
        message_dialog = Gtk.MessageDialog(
            modal=True,
            destroy_with_parent=True,
            transient_for=self.b.get_object("mainWindow"),
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text="This action will cause the stage to move."
        )
        message_dialog.format_secondary_text("Before clicking OK, check that all foreign objects are clear from the stage area and that it is safe to move.")

        result = message_dialog.run()
        message_dialog.destroy()
        return(result)


    def on_home_button(self, button):
        """Home the stage."""
        if (self.move_warning() == Gtk.ResponseType.OK):
            lg.info("Requesting stage home...")
            msg = {'cmd':'home', 'pcb':self.config['controller']['address'], 'stage_uri':self.config['stage']['uri']}
            pic_msg = pickle.dumps(msg, protocol=pickle.HIGHEST_PROTOCOL)
            self.mqttc.publish("cmd/util", pic_msg, qos=2).wait_for_publish()


    def on_halt_button(self, button):
        """Emergency stop"""
        lg.warning("Powering down the stage motor drivers")
        msg = {'cmd':'estop', 'pcb':self.config['controller']['address']}
        pic_msg = pickle.dumps(msg, protocol=pickle.HIGHEST_PROTOCOL)
        self.mqttc.publish("cmd/uitl", pic_msg, qos=2).wait_for_publish()
        self.mqttc.publish("gui/stop", "stop", qos=2).wait_for_publish()


    def on_stage_read_button(self, button):
        """Read the current stage position."""
        msg = {'cmd':'read_stage', 'pcb':self.config['controller']['address'], 'stage_uri':self.config['stage']['uri']}
        pic_msg = pickle.dumps(msg, protocol=pickle.HIGHEST_PROTOCOL)
        self.mqttc.publish("cmd/util", pic_msg, qos=2).wait_for_publish()


    def on_goto_button(self, button):
        """Goto stage position."""
        if (self.move_warning() == Gtk.ResponseType.OK):
            lg.debug("Sending the stage some place")
            pos = [self.gotos[0].get_value()]
            if self.num_axes >= 2:
                pos += [self.gotos[1].get_value()]
            if self.num_axes >= 3:
                pos += [self.gotos[2].get_value()]
            msg = {'cmd':'goto', 'pos':pos, 'pcb':self.config['controller']['address'], 'stage_uri':self.config['stage']['uri']}
            pic_msg = pickle.dumps(msg)
            self.mqttc.publish("cmd/util", pic_msg, qos=2).wait_for_publish()


    def on_run_button(self, button):
        """Send run info to experiment orchestrator via MQTT."""
        if (self.move_warning() == Gtk.ResponseType.OK):
            self.b.get_object("run_but").set_sensitive(False)  # prevent multipress
            run_name = self.b.get_object("run_name").get_text()
            lg.info(f"Starting new run: {run_name}")

            msg = {"cmd":"run", "args": self.gui_to_args(self.harvest_gui_data()), "config": self.config}
            pic_msg = pickle.dumps(msg, protocol=pickle.HIGHEST_PROTOCOL)
            self.mqttc.publish("measurement/run", pic_msg, qos=2).wait_for_publish()


    # makes the gui dict more consumable for a backend
    def gui_to_args(self, gui_dict):
        args = {}
        for key, val in gui_dict.items():
            args[key] = val['value']
        if args['v_dwell_check'] == False:
            args['v_dwell'] = 0
        if args['mppt_check'] == False:
            args['mppt_dwell'] = 0
        if args['i_dwell_check'] == False:
            args['i_dwell'] = 0
        args['chan1'] = args['chan1_ma']/1000
        args['chan2'] = args['chan2_ma']/1000
        args['chan3'] = args['chan3_ma']/1000
        args['i_dwell_value'] = args['i_dwell_value_ma']/1000
        for i, lab in enumerate(args['label_tree']):
            if lab == '':
                args['label_tree'][i] = self.substrate_designators[i]
        return(args)


    def on_cal_eqe_button(self, button):
        """Measure EQE calibration photodiode."""
        """Send run info to experiment orchestrator via MQTT."""
        if (self.move_warning() == Gtk.ResponseType.OK):
            self.b.get_object("run_but").set_sensitive(False)  # prevent run
            lg.info(f"Starting EQE calibration")

            msg = {"cmd":"run", "args": self.gui_to_args(self.harvest_gui_data()), "config": self.config}
            pic_msg = pickle.dumps(msg, protocol=pickle.HIGHEST_PROTOCOL)

            self.mqttc.publish("measurement/calibrate_eqe", pic_msg, qos=2).wait_for_publish()
            # check for calibration/eqe timestamp


    # TODO: combine buttons
    def on_cal_psu_button(self, button):
        """Measure EQE calibration photodiode."""
        """Send run info to experiment orchestrator via MQTT."""
        if (self.move_warning() == Gtk.ResponseType.OK):
            self.b.get_object("run_but").set_sensitive(False)  # prevent run
            lg.info(f"Starting bias light LED calibration")

            msg = {"cmd":"run", "args": self.gui_to_args(self.harvest_gui_data()), "config": self.config}
            pic_msg = pickle.dumps(msg, protocol=pickle.HIGHEST_PROTOCOL)

            self.mqttc.publish("measurement/calibrate_psu", pic_msg, qos=2).wait_for_publish()


    def on_smart_mode_activate(self, button):
        self.update_gui()


    def row_act(self, a):
        pass

    def cur_row(self, a):
        pass

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


if __name__ == "__main__":
    app = App()
    app.run(sys.argv)
