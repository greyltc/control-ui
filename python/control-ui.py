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

import yaml

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
        self.run_handler_status = 'Offline'
        self.eqe_cal_time = None
        self.psu_cal_time = None
        self.iv_cal_time = None

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
        try:
            rs = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"[: number_list[1]]
            cs = range(number_list[0])
            cs = [str(x+1) for x in cs]
        except IndexError:
            # if number of columns not given, must be 0
            rs = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"[: number_list[0]]
            cs = ['']

        subdes = [f"{n}{m}" for m in rs for n in cs]

        return subdes


    def _start_mqtt(self):
        """Start the MQTT client and subscribe to the CLI topic."""
        self.mqtt_connecting = True

        def on_message(mqttc, obj, msg):
            """Act on an MQTT message."""
            try:
                m = pickle.loads(msg.payload)
            except:
                m = None

            # examine by message topic
            if m is not None:
                if (msg.topic) == "measurement/status":
                    self.run_handler_status = m
                    if m == 'Ready':
                        self.b.get_object("run_but").set_sensitive(True)
                    if m == 'Offline' or m == 'Busy':
                        self.b.get_object("run_but").set_sensitive(False)  # prevent multipress
                elif (msg.topic) == "measurement/log":
                    lg.log(m["level"], m["msg"])
                elif (msg.topic) == "calibration/eqe":
                    self.eqe_cal_time = m['timestamp']
                elif (msg.topic) == "calibration/spectrum":
                    self.iv_cal_time = m['timestamp']
                elif "calibration/psu" in msg.topic:
                    self.psu_cal_time = m['timestamp']

                # examine by message content
                if 'log' in m:  # log update message
                    lg.log(m['log']['level'], m['log']['text'])
                if 'pos' in m:  # position update message
                    pos = m['pos']
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

            # what state the measurement backend is in
            self.mqttc.subscribe("measurement/status/#", qos=2)

            # log messages from the measurement backend
            self.mqttc.subscribe("measurement/log/#", qos=2)

            # log messages from the calibration backend
            self.mqttc.subscribe("calibration/#", qos=2)

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


    def setup_dev_stores(self, num_substrates, num_pix, dev_stores, substrate_store):
        for store in dev_stores:
            checked = True
            inconsistent = False
            store.clear()
            top = store.append(None, ['All', checked, inconsistent])
            # fill in the model
            for i in range(num_substrates):  # iterate through substrates 
                if substrate_store[i][0] == "":
                    label = substrate_store[i][1]
                else:
                    label = substrate_store[i][0]
                piter = store.append(top, [label, checked, inconsistent])
                j = 1
                while j <= num_pix[i]:  # iterate through pixels on that substrate 
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
        # colCheck.set_clickable(True)
        # colCheck.connect("clicked", self.dev_col_click)
        deviceTree.append_column(colCheck)

        # connect the cellrenderertoggle with a callback function
        renderCheck.connect("toggled", self.dev_toggle)

        deviceTree.connect("key-release-event", self.handle_dev_key)

    # handles clicks on the header cell in the device tree selector
    #def dev_col_click(self, a):
    #    lg.debug(f"Col Clicked! {a}")


    # callback function for select/deselect device/substrate
    def dev_toggle(self, toggle, path):
        eqe = "eqe" in Gtk.Buildable.get_name(self.po.get_relative_to())
        this = self.dev_store[eqe].get_iter(path)
        path_split = path.split(":")
        old_value = self.dev_store[eqe][this][1] # what was the checkbox state when we clicked it?
        current_value = not old_value  # compute what the new checkbox state should be
        self.dev_store[eqe][this][1] = current_value  # update the checkbox state
        self.dev_store[eqe][this][2] = False  # we just clicked it. so there's no way it can be inconsistent
        if len(path_split) == 1:  # the toplevel checkbox was toggled
            siter = self.dev_store[eqe].iter_children(this) # substrate iterator
            while siter is not None:  # iterate through the substrates
                self.dev_store[eqe][siter][1] = current_value
                self.dev_store[eqe][siter][2] = False
                diter = self.dev_store[eqe].iter_children(siter) # device iterator
                while diter is not None:  # iterate through the devices
                    self.dev_store[eqe][diter][1] = current_value
                    self.dev_store[eqe][diter][2] = False
                    diter = self.dev_store[eqe].iter_next(diter)
                siter = self.dev_store[eqe].iter_next(siter)
        if len(path_split) == 2:  # if length of the path is 2 (that is, if we are selecting a substrate)
            citer = self.dev_store[eqe].iter_children(this) # get the iter associated with my first child
            # change the state of all children to match this one
            while citer is not None:
                self.dev_store[eqe][citer][1] = current_value
                citer = self.dev_store[eqe].iter_next(citer)
        elif len(path_split) == 3: # a device toggle
            # get the first child of the parent of the substrate (device 1)
            piter = self.dev_store[eqe].iter_parent(this) # parent iterator
            citer = self.dev_store[eqe].iter_children(piter) # iterator for the first sibling of my parent
            # check if all the children are selected
            num_selected = 0
            num_deselected = 0
            while citer is not None:
                if self.dev_store[eqe][citer][1] is True:
                    num_selected = num_selected + 1
                else:
                    num_deselected = num_deselected + 1
                citer = self.dev_store[eqe].iter_next(citer)
            # if they are, the device as well is selected; otherwise it is not
            if num_deselected == 0: # all siblings selected, then substrate selected, consitent
                self.dev_store[eqe][piter][2] = False
                self.dev_store[eqe][piter][1] = True
            elif num_selected == 0:  # all siblings deselected, then substrate deselected, consitent
                self.dev_store[eqe][piter][2] = False
                self.dev_store[eqe][piter][1] = False
            else:
                self.dev_store[eqe][piter][2] = True  # not all selected, not all deselected, thus substrate is incosistent

        # iterate through everything and build up the resulting bitmask for the text field
        siter = self.dev_store[eqe].get_iter("0:0")  # iterator for first substrate
        selection_bitmask = 0
        bit_location = 0
        total_enabled = 0
        total_disenabled = 0
        while siter is not None:
            diter = self.dev_store[eqe].iter_children(siter)  # device iterator
            while diter is not None:
                if self.dev_store[eqe][diter][1] is True:
                    selection_bitmask = selection_bitmask + (1 << bit_location)
                    total_enabled += 1
                else:
                    total_disenabled += 1
                bit_location = bit_location + 1
                diter = self.dev_store[eqe].iter_next(diter)  # advance to the next device
            siter = self.dev_store[eqe].iter_next(siter)  # advance to the next substrate

        # figure out what to do with the All checkbox
        top = self.dev_store[eqe].get_iter("0")
        if total_enabled == 0:
            self.dev_store[eqe][top][1] = False  # set top off
            self.dev_store[eqe][top][2] = False  # set top consistent
        elif total_disenabled == 0:
            self.dev_store[eqe][top][1] = True  # set top on
            self.dev_store[eqe][top][2] = False  # set top consistent
        else:
            self.dev_store[eqe][top][2] = True  # set top inconsistant

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
            #self.last_valid_devs[eqe] = text_is
            #editable.set_icon_from_icon_name(0, "emblem-default")
            #num_selected = sum([c == "1" for c in bin(selection_bitmask)])
            #txt = f'Device Selection Bitmask ({num_selected} selected)'
            if eqe:
                self.update_measure_count(True)
                #self.b.get_object("eqe_dsb_lab").set_text(txt)
                #self.measure_note(selection_bitmask, self.approx_seconds_per_eqe)
            else:
                self.update_measure_count(False)
                #self.b.get_object("iv_dsb_lab").set_text(txt)
                #self.measure_note(selection_bitmask, self.approx_seconds_per_iv)
        else:
            editable.set_icon_from_icon_name(0, "dialog-error")

    def update_measure_count(self, eqe):
        if eqe == True:
            thing = "eqe"
        else:
            thing = "iv"
        text_is = self.b.get_object(f"{thing}_devs").get_text()
        selection_bitmask = int(text_is, 16)
        num_selected = sum([c == "1" for c in bin(selection_bitmask)])
        txt = f'Device Selection Bitmask ({num_selected} selected)'
        self.b.get_object(f"{thing}_dsb_lab").set_text(txt)

    # log message printer for device selection change
    def measure_note(self, selection_bitmask, seconds_per):
        pass
        #num_selected = sum([c == "1" for c in bin(selection_bitmask)])
        #eqe_dsb_lab = self.b.get_object("eqe_dsb_lab")
        #eqe_dsb_lab.set_text(f'Device Selection Bitmask ({num_selected} selected)')

        #duration_string = humanize.naturaldelta(
        #    dt.timedelta(seconds=seconds_per * num_selected)
        #)

        # TODO: look at making this easier to maintain
        # This report is probably too annoying to maintain properly in its current state
        #lg.info(f"{num_selected} devices selected for ~ {duration_string}")


    def setup_substrate_tree(self, labels, substrate_designators, cell_y_padding, layouts):
        substrate_tree = self.b.get_object("substrate_tree")
        substrate_store = Gtk.ListStore(str, str, int, str)
        self.label_shadow = labels

        # the refrence designator column
        ref_des_cell = Gtk.CellRendererText()
        ref_des_col = Gtk.TreeViewColumn("Slot", ref_des_cell, text=1, ypad=2)
        if substrate_tree.get_columns() == []:
            substrate_tree.append_column(ref_des_col)

        # the editable substrate label column
        label_cell = Gtk.CellRendererText()
        label_cell.set_property("editable", True)
        labels_col = Gtk.TreeViewColumn("Label", label_cell, text=0, ypad=2)
        # only append the col if it's not already there
        # fixes double col on file load
        if len(substrate_tree.get_columns()) == 1:
            substrate_tree.append_column(labels_col)

        # the layout column
        layout_cell = Gtk.CellRendererCombo()
        layouts_store = Gtk.ListStore(str)
        for layout in self.layouts:
            layouts_store.append([layout])
        layout_cell.set_property("editable", True)
        layout_cell.set_property("model", layouts_store)
        layout_cell.set_property("has-entry", False)
        layout_cell.set_property("text-column", 0)
        layout_cell.connect("edited", self.on_layout_combo_changed)
        layout_col = Gtk.TreeViewColumn("Layout", layout_cell, text=3, ypad=2)
        if len(substrate_tree.get_columns()) == 2:
            substrate_tree.append_column(layout_col)

        for i in range(self.num_substrates):
            substrate_store.append([labels[i], substrate_designators[i], cell_y_padding[i], layouts[i]])
        substrate_tree.set_model(substrate_store)

        label_cell.connect("edited", self.store_substrate_label)
        #substrate_tree.connect("key-release-event", self.handle_label_key)

        return (substrate_tree, substrate_store)

    def on_layout_combo_changed(self, widget, path, text):
        self.substrate_store[path][3] = text


    # handles keystroke in the label creation tree
    # def handle_label_key(self, tv, event):
    #     keyname = Gdk.keyval_name(event.keyval)
    #     if keyname in ["Return", "Enter"]:
    #         path, col = self.label_tree.get_cursor()
    #         path.next()
    #         self.label_tree.set_cursor_on_cell(
    #             path, focus_column=col, focus_cell=None, start_editing=True
    #         )


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
        dev_path = f'0:{path}'
        self.label_shadow[int(path)] = text
        self.substrate_store[path][0] = text
        if text == "": # if it's empty use the default
            self.dev_store[0][dev_path][0] = self.substrate_store[path][1]
            self.dev_store[1][dev_path][0] = self.substrate_store[path][1]
        else: # otherwise use the user's one
            self.dev_store[0][dev_path][0] = self.substrate_store[path][0]
            self.dev_store[1][dev_path][0] = self.substrate_store[path][0]


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
            hb = self.b.get_object("headerBar")
            if self.mqttc.is_connected():
                status = f"Connected | {self.run_handler_status}"
                self.mqtt_connected = True
            else:
                status = f"Disconnected"
                self.mqtt_connected = False
            hb.set_subtitle(f"Status: {status}")
        else:
            self.mqtt_connected = False
            status = "Disconnected"
            self.b.get_object("headerBar").set_subtitle(f"Status: {status}")
            if self.mqtt_connecting == False: # don't spam connections
                self._start_mqtt()
        if self.eqe_cal_time is None:
            human_dt = 'No record'
        else:
            human_dt = humanize.naturaltime(dt.datetime.now() - dt.datetime.fromtimestamp(self.eqe_cal_time))
        self.b.get_object("eqe_cal_label").set_text(f"EQE Cal. Age: {human_dt}")
        if self.iv_cal_time is None:
            human_dt = 'No record'
        else:
            human_dt = humanize.naturaltime(dt.datetime.now() - dt.datetime.fromtimestamp(self.iv_cal_time))
        self.b.get_object("iv_cal_label").set_text(f"Solar Sim Intensity Cal. Age: {human_dt}")
        if self.psu_cal_time is None:
            human_dt = 'No Record'
        else:
            human_dt = humanize.naturaltime(dt.datetime.now() - dt.datetime.fromtimestamp(self.psu_cal_time))
        self.b.get_object("psu_cal_label").set_text(f"EQE Bias Light Cal. Age: {human_dt}")
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

            example_config_file_name = "example_config.yaml"
            config_file_name = "measurement_config.yaml"
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
                with open(self.config_file, "r") as f:
                    self.config = yaml.load(f, Loader=yaml.FullLoader)
            except:
                lg.error("Unexpected error parsing config file.")
                lg.error(sys.exc_info()[0])
                raise

            # get dimentions of substrate array to generate designators
            number_list = self.config["substrates"]["number"]
            self.substrate_designators = self._generate_substrate_designators(
                number_list
            )
            self.num_substrates = len(self.substrate_designators)

            # handle custom locations and stage stuff
            pl = self.b.get_object("places_list")  # a tree model
            self.custom_coords = []
            enable_stage = False
            if 'stage' in self.config:
                if "enabled" in self.config["stage"]:
                    if self.config["stage"]["enabled"] == True:
                        enable_stage = True

                if "experiment_positions" in self.config["stage"]:
                    for key, val in self.config["stage"]["experiment_positions"].items():
                        pl.append(['EXPERIMENT -- ' + key])
                        self.custom_coords.append(val)

                if "custom_positions" in self.config["stage"]:
                    for key, val in self.config["stage"]["custom_positions"].items():
                        pl.append([key])
                        self.custom_coords.append(val)

            if enable_stage == True:
                if len(self.custom_coords) == 0:
                    self.b.get_object("places_combo").set_visible(False)
                    self.b.get_object("places_label").set_visible(False)

                # stage specific stuff
                stage_uri_split = self.config["stage"]["uri"].split('://')
                stage_protocol = stage_uri_split[0]
                stage_address = stage_uri_split[1]
                stage_address_split = stage_address.split('/')
                esl = stage_address_split[0]
                steps_per_mm = int(stage_address_split[1])
                if ',' in esl:
                    esl = [float(x) for x in esl.split(',')]
                else:
                    esl = [float(esl)]
                length_oom = max([math.ceil(math.log10(x)) for x in esl])
                
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
            else: # there is no stage
                self.b.get_object("stage_util").set_visible(False)
                self.b.get_object("home_util").set_visible(False)

            # do lockin things
            enable_lia = False
            if 'lia' in self.config:
                if "enabled" in self.config["lia"]:
                    if self.config["lia"]["enabled"] == True:
                        enable_lia = True

            # do mono things
            enable_mono = False
            if 'monochromator' in self.config:
                if "enabled" in self.config["monochromator"]:
                    if self.config["monochromator"]["enabled"] == True:
                        enable_mono = True

            # do smu things
            enable_smu = False
            if 'smu' in self.config:
                if "enabled" in self.config["smu"]:
                    if self.config["smu"]["enabled"] == True:
                        enable_smu = True

            # do solarsim things
            enable_solarsim = False
            if 'solarsim' in self.config:
                if "enabled" in self.config["solarsim"]:
                    if self.config["solarsim"]["enabled"] == True:
                        enable_solarsim = True

            # do psu things
            enable_psu = False
            if 'psu' in self.config:
                if "enabled" in self.config["psu"]:
                    if self.config["psu"]["enabled"] == True:
                        enable_solarsim = True

            enable_eqe = False
            if (enable_mono == True) and (enable_lia == True) and (enable_smu == True):
                enable_eqe = True
            
            enable_iv = False
            if (enable_smu == True):
                enable_iv = True

            if enable_eqe == False:
                self.b.get_object("eqe_frame").set_visible(False)
                self.b.get_object("eqe_util").set_visible(False)
                self.b.get_object("eqe_wv").set_visible(False)
            else:
                if enable_psu == False:
                    self.b.get_object("psu_frame").set_visible(False)

            if enable_iv == False:
                self.b.get_object("iv_frame").set_visible(False)
                self.b.get_object("vt_wv").set_visible(False)
                self.b.get_object("iv_wv").set_visible(False)
                self.b.get_object("mppt_wv").set_visible(False)
                self.b.get_object("jt_wv").set_visible(False)
            else:
                if enable_solarsim == False:
                    self.b.get_object("ss_box").set_visible(False)
                    self.b.get_object("ill_box").set_visible(False)

            if (enable_smu == False) or (enable_psu == False):
                self.b.get_object("bias_light_util").set_visible(False)

            if (enable_iv == False) or (enable_eqe == False):
                self.b.get_object("eqe_relay_util").set_visible(False)

            if (enable_mono == False):
                self.b.get_object("mono_util").set_visible(False)

            # do layout things
            self.layouts = []  # list of enabled layouts
            self.npix = []  # number of pixels for each layout
            if 'substrates' in self.config:
                if 'layouts' in self.config['substrates']:
                    for layout_name, val in self.config['substrates']['layouts'].items():
                        if 'enabled' in val:
                            if val['enabled'] == True:
                                self.layouts.append(layout_name)
                                self.npix.append(len(val['pixels']))

            ns = self.num_substrates
            self.substrate_tree, self.substrate_store = self.setup_substrate_tree(['']*ns, self.substrate_designators, [0]*ns, [self.layouts[0]]*ns)

            # one for EQE, one for iv
            # [str, bool, bool] is for [label, checked, inconsistent]
            self.dev_store = [
                Gtk.TreeStore(str, bool, bool),
                Gtk.TreeStore(str, bool, bool),
            ]  # [iv devs, eqe devs]
            self.spix = [self.npix[0]]*self.num_substrates  # list containing the number of pixels each substrate has
            self.setup_dev_stores(self.num_substrates, self.spix, self.dev_store, self.substrate_store)

            self.dev_tree = self.b.get_object("devTV")
            self.setup_dev_tree(self.dev_tree)

            # TODO: do this in a non-obsolete way (probably w/ css somehow)
            fontdesc = Pango.FontDescription("monospace")

            abs_max_devices = max(self.npix) * self.num_substrates
            max_devices = self.npix[0] * self.num_substrates
            max_address_string_length = math.ceil(abs_max_devices / 4)
            selection_box_length = max_address_string_length + 3

            self.def_fmt_str = f"0{max_address_string_length}X"

            default_on = f"0x{int('1'*max_devices,2):{self.def_fmt_str}}"
            default_off = f"0x{0:{self.def_fmt_str}}"

            self.iv_dev_box = self.b.get_object("iv_devs")

            self.last_valid_devs = [default_on, default_off]  # [iv devs, eqe devs]

            self.iv_dev_box = self.b.get_object("iv_devs")
            self.iv_dev_box.modify_font(fontdesc)
            self.iv_dev_box.set_text(default_on)
            self.iv_dev_box.set_width_chars(selection_box_length)
            #self.iv_dev_box.set_icon_from_icon_name(0, "emblem-default")

            self.eqe_dev_box = self.b.get_object("eqe_devs")
            self.eqe_dev_box.modify_font(fontdesc)
            self.eqe_dev_box.set_text(default_off)
            self.eqe_dev_box.set_width_chars(selection_box_length)
            #self.eqe_dev_box.set_icon_from_icon_name(0, "emblem-default")

            self.po = self.b.get_object("picker_po")
            self.po.set_position(Gtk.PositionType.BOTTOM)

            self.approx_seconds_per_iv = 50
            self.approx_seconds_per_eqe = 150

            cvt_vis = False
            self.wvids = []
            self.wvids.append("vt_wv")
            self.wvids.append("iv_wv")
            self.wvids.append("mppt_wv")
            self.wvids.append("jt_wv")
            self.wvids.append("eqe_wv")

            self.uris = []  # the uris to put in the webviews
            if "network" in self.config:
                if "live_data_uris" in self.config["network"]:
                    for uri in self.config["network"]['live_data_uris']:
                        self.uris.append(uri)
                
                    if len(self.config["network"]['live_data_uris']) == 6:
                        cvt_vis = True

            # set the custom view tab visible or not
            self.b.get_object("custom_wv").set_visible(cvt_vis)

            # start MQTT client
            self._start_mqtt()

            # send config file to CLI MQTT client
            # not really sure why this should go out now...
            # with open(self.config_file, "r") as f:
            #     payload = json.dumps(f.read())
            # self.mqttc.publish("gui/config", payload, qos=2)

            # update the devices to measure text for both eqe and iv
            self.update_measure_count(True)
            self.update_measure_count(False)

            # read the default recipe from the config and set the gui box to that
            if "solarsim" in self.config:
                if "recipes" in self.config["solarsim"]:
                    tb = self.b.get_object('light_recipe') # the active (editable combox box item)
                    tb.set_text(self.config["solarsim"]["recipes"][0])
                    tbc = self.b.get_object('light_recipe_combo')
                    for recipe in self.config["solarsim"]["recipes"]:
                        tbc.append_text(recipe)

            # read the default recipe from the config and set the gui box to that
            if "mppt" in self.config:
                if "presets" in self.config["mppt"]:
                    tb = self.b.get_object('mppt_params')
                    tb.set_text(self.config["mppt"]["presets"][0])
                    tbc = self.b.get_object('mppt_params_combo')
                    for recipe in self.config["mppt"]["presets"]:
                        tbc.append_text(recipe)

            # read the invert plot settings from the config and set the switches to that
            if 'plots' in self.config:
                if 'invert_voltage' in self.config['plots']:
                    sw = self.b.get_object('inv_v_switch')
                    sw.set_active(self.config['plots']['invert_voltage'])
                if 'invert_current' in self.config['plots']:
                    sw = self.b.get_object('inv_i_switch')
                    sw.set_active(self.config['plots']['invert_current'])

            # make sure the plotter is in sync with us when we start
            self.on_plotter_switch(None, True)
            self.on_voltage_switch(None, self.b.get_object('inv_v_switch').get_active())
            self.on_current_switch(None, self.b.get_object('inv_i_switch').get_active())

            # set bias led spinbox limits
            if 'psu' in self.config:
                if 'ch1_ocp' in self.config['psu']:
                    c1bla = self.b.get_object('ch1_bias_light_adj')
                    c1bla.set_upper(self.config['psu']['ch1_ocp']*1000)
                if 'ch2_ocp' in self.config['psu']:
                    c2bla = self.b.get_object('ch2_bias_light_adj')
                    c2bla.set_upper(self.config['psu']['ch2_ocp']*1000)
                if 'ch3_ocp' in self.config['psu']:
                    c3bla = self.b.get_object('ch3_bias_light_adj')
                    c3bla.set_upper(self.config['psu']['ch3_ocp']*1000)

            self.tick()
            self.ticker_id = GLib.timeout_add_seconds(1, self.tick, None)
            self.b.connect_signals(self)  # maps all ui callbacks to functions here

            # for doing tasks on stack change
            ms = self.b.get_object('mainStack')
            ms.connect("notify::visible-child", self.on_stack_change)

            # for handling global accelerator key combos
            ag = self.b.get_object('global_keystrokes')
            ag.connect(Gdk.keyval_from_name('D'), Gdk.ModifierType.CONTROL_MASK, 0, self.on_debug_button)

            self.main_win = self.b.get_object("mainWindow")
            self.main_win.set_application(self)


        self.main_win.present()

    def load_live_data_webviews(self, load):
        for i,wvid in enumerate(self.wvids):
            wv = self.b.get_object(wvid)
            if wv.get_visible() == True:
                wv.stop_loading()
                if load == True:
                    wv.load_uri(self.uris[i])
                else:
                    wv.load_html("Inactive.")


    # must only get called if this tab is visible
    def load_custom_webview(self, load):
        wv = self.b.get_object("custom_wv")
        if wv.get_visible() == True:
            wv.stop_loading()
            if load == True:
                wv.load_uri(self.uris[-1])  # should always be the last webview and uri
            else:
                wv.load_html("Inactive.")

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

    def on_debug_button(self, *args, **kw_args):
        lg.debug("Hello World!")
        self.b.get_object("run_but").set_sensitive(True)
        self.load_live_data_webviews(load=False)
        msg = {'cmd':'debug'}
        pic_msg = pickle.dumps(msg, protocol=pickle.HIGHEST_PROTOCOL)
        self.mqttc.publish("cmd/uitl", pic_msg, qos=2).wait_for_publish()

    # sets up the device selection tree based on the text in the selection box
    def on_devs_icon_release(self, entry, icon, user_data=None):
        eqe = "eqe" in Gtk.Buildable.get_name(entry)
        self.dev_tree.set_model(self.dev_store[eqe])
        sw = self.dev_tree.get_parent()  # scroll window
        sw.set_min_content_height((self.num_substrates + 2) * 26)
        if entry.get_icon_name(0) != "emblem-default":
            entry.set_text(self.last_valid_devs[eqe])
        text_is = entry.get_text()
        selection_bitmask = int(text_is, 16)

        bin_mask = bin(selection_bitmask)[2::]
        bin_mask_rev = bin_mask[::-1]

        # iterate through everything and build up the result
        siter = self.dev_store[eqe].get_iter("0:0")  # first substrate iterator
        bit_location = 0
        total_enabled = 0
        total_disabled = 0
        while siter is not None:
            num_enabled = 0  # keeps track of number of enabled devices on this substrate
            num_disabled = 0
            diter = self.dev_store[eqe].iter_children(siter)  # device iterator
            while diter is not None:
                if (bit_location + 1 <= len(bin_mask_rev)) and (bin_mask_rev[bit_location] == "1"):
                    self.dev_store[eqe][diter][1] = True
                    num_enabled += 1
                else:
                    self.dev_store[eqe][diter][1] = False
                    num_disabled += 1
                bit_location = bit_location + 1
                diter = self.dev_store[eqe].iter_next(diter)  # advance to the next device
            if num_enabled == 0:
                self.dev_store[eqe][siter][1] = False  # set substrate off
                self.dev_store[eqe][siter][2] = False  # set substrate consistent
            elif num_disabled == 0:
                self.dev_store[eqe][siter][1] = True  # set substrate on
                self.dev_store[eqe][siter][2] = False  # set substrate consistent
            else:
                self.dev_store[eqe][siter][2] = True  # set substrate inconsistant
            siter = self.dev_store[eqe].iter_next(siter)  # advance to the next substrate
            total_enabled += num_enabled
            total_disabled += num_disabled
        
        # figure out what to do with the All checkbox
        top = self.dev_store[eqe].get_iter("0")
        self.dev_tree.expand_row(Gtk.TreePath("0"), False)  # top is always expanded
        if total_enabled == 0:
            self.dev_store[eqe][top][1] = False  # set top off
            self.dev_store[eqe][top][2] = False  # set top consistent
        elif total_disabled == 0:
            self.dev_store[eqe][top][1] = True  # set top on
            self.dev_store[eqe][top][2] = False  # set top consistent
        else:
            self.dev_store[eqe][top][2] = True  # set top inconsistant

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
        self.mqttc.publish(
            "measurement/stop", pickle.dumps("stop"), qos=2
        ).wait_for_publish()

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
                elif isinstance(this_obj, gi.overrides.Gtk.TreeView):  # the TreeViews are unfortunately not pickleable
                    ls = this_obj.get_model()  # list store associated with the treeview
                    table_list = []  # list we'll use to store the list store
                    col_types = []
                    if ls is not None:
                        n_cols = ls.get_n_columns()
                        for i in range(n_cols):
                            col_types.append(ls.get_column_type(i).name)
                        for row in ls:
                            table_list.append(tuple(row))
                    gui_data[id_str] = {"type": str(type(this_obj)), "value": {"col_types": col_types, "table_list": table_list}, "call_to_set": None}
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
                if 'TreeView' in obj_info['type']:
                    tvd = obj_info['value']
                    if len(tvd['col_types']) != 0:
                        cts = tvd['col_types']
                        data = tvd['table_list']
                        if id_str == 'substrate_tree':
                            labels = [x[0] for x in data]
                            ref_des = [x[1] for x in data]
                            y_pad = [x[2] for x in data]
                            if len(data[0]) > 3:
                                layouts = [x[3] for x in data]
                            else:
                                layouts = ['Unknown' for x in data]
                            try:
                                self.substrate_tree, self.substrate_store = self.setup_substrate_tree(labels, ref_des, y_pad, layouts)
                                for i, lab in enumerate(labels):
                                    self.store_substrate_label(None, str(i), lab)
                            except:
                                lg.info(f"Loading substrate info failed.")
                else:
                    this_obj = self.b.get_object(id_str)
                    call_to_set = getattr(this_obj, obj_info['call_to_set'])
                    call_to_set(obj_info['value'])
            self.update_gui()
        else:
            lg.info(f"Load aborted.")

    def on_round_robin_button(self, button):
        button_label = button.get_label()
        this_type = "none"
        if button_label == "Connectivity":
            lg.info("Connectivity check started...")
            this_type = "connectivity"
        elif 'RTD' in button_label:
            lg.info("Measuring RTD temperatures...")
            this_type = "rtd"
        iv_dev_txt = self.b.get_object("iv_devs").get_text()
        iv_dev_num = int(iv_dev_txt, 16)
        eqe_dev_txt = self.b.get_object("eqe_devs").get_text()
        eqe_dev_num = int(eqe_dev_txt, 16)
        any_dev_num = iv_dev_num|eqe_dev_num # combine the selections for the connectivity check
        some_lists = self.bitmask_to_some_lists(hex(any_dev_num))
        msg = {
            'cmd':'round_robin',
            'type': this_type,
            'slots': some_lists['subs_names'],
            'pixels': some_lists['sub_dev_nums'],
            'pcb': self.config['controller']['address'],
            'smu': self.config['smu']
            }
        pic_msg = pickle.dumps(msg, protocol=pickle.HIGHEST_PROTOCOL)
        self.mqttc.publish("cmd/uitl", pic_msg, qos=2).wait_for_publish()


    def on_device_toggle(self, button):
        """
        allows the user to connect any one pixel, or disconnect them all
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
        tpix = sum(self.spix)  # total pixels
        selection_bitmask = int(bitmask, 16)
        bin_mask = bin(selection_bitmask)[2::]
        bin_mask = bin_mask[:tpix]  # prune to total pixel number
        bin_mask_rev = bin_mask[::-1]
        dev_nums = []
        subs_nums = []
        sub_dev_nums = []
        subs_names = []
        user_labels = []
        selections = []
        #for subs in 
        for i,c in enumerate(bin_mask_rev):
            if c == '1':
                dev_num = i
                dev_nums += [dev_num]
                subs_num = math.floor(i/self.num_pix)  #TODO this is wrong. needs to be thought now that we don't have equal numbers of pixels per substrate
                subs_nums += [subs_num]
                sub_dev_num = dev_num%self.num_pix + 1  # we'll count these from 1 here #TODO this is wrong. needs to be thought now that we don't have equal numbers of pixels per substrate
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
        'smu_le': self.config['smu']['terminator'],
        'lia_address': self.config['lia']['address'],
        'mono_address': self.config['monochromator']['address'],
        'le_address': self.config['solarsim']['uri'],
        'le_recipe': self.b.get_object("light_recipe").get_text()
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
            text="This action may cause the stage to move."
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
        self.mqttc.publish("measurement/stop", "stop", qos=2).wait_for_publish()


    def on_mono_zero_button(self, button):
        """Sends Monochromator to 0nm"""
        msg = {'cmd':'mono_zero', 'mono_address': self.config['monochromator']['address']}
        pic_msg = pickle.dumps(msg, protocol=pickle.HIGHEST_PROTOCOL)
        self.mqttc.publish("cmd/util", pic_msg, qos=2).wait_for_publish()


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
            run_name = self.b.get_object("run_name").get_text()
            gui_data = self.harvest_gui_data()

            autosave_config = True
            if 'config_autosave' in self.config:
                if 'enabled' in self.config['config_autosave']:
                    if self.config['config_autosave']['enabled'] != True:
                       autosave_config = False
                    else:
                        if 'path' in self.config['config_autosave']:
                            user_autosave_path = self.config['config_autosave']['path']
            
            if autosave_config == True:
                autosave_file_name = run_name + '_autosave.dat'
                if 'user_autosave_path' in locals():
                    if os.path.isabs(user_autosave_path):
                        autosave_pathname = pathlib.Path(user_autosave_path)
                    else:
                        autosave_pathname = pathlib.Path.home() / user_autosave_path
                else:
                    autosave_pathname = pathlib.Path.home()
                autosave_pathname.mkdir(parents=True, exist_ok=True)
                autosave_destination = (autosave_pathname / autosave_file_name)
                lg.info(f"Autosaving gui state to: {autosave_destination}")
                with open(autosave_destination, "wb") as f:
                    pickle.dump(gui_data, f, protocol=pickle.HIGHEST_PROTOCOL)

            msg = {"cmd":"run", "args": self.gui_to_args(gui_data), "config": self.config}
            pic_msg = pickle.dumps(msg, protocol=pickle.HIGHEST_PROTOCOL)
            # publish the run message
            lg.info(f"Starting new run: {run_name}")
            self.b.get_object("run_but").set_sensitive(False)  # prevent multipress
            self.mqttc.publish("measurement/run", pic_msg, qos=2).wait_for_publish()


    # makes the gui dict more consumable for a backend
    def gui_to_args(self, gui_dict):
        args = {}
        #return(args)
        for key, val in gui_dict.items():
            args[key] = val['value']
        if args['v_dwell_check'] == False:
            args['v_dwell'] = 0
        if args['mppt_check'] == False:
            args['mppt_dwell'] = 0
        if args['i_dwell_check'] == False:
            args['i_dwell'] = 0
        if args['ad_switch'] == True:
            args['source_delay'] = -1
        args['chan1'] = args['chan1_ma']/1000
        args['chan2'] = args['chan2_ma']/1000
        args['chan3'] = args['chan3_ma']/1000
        args['i_dwell_value'] = args['i_dwell_value_ma']/1000

        args['substrate_labels'] = []
        args['substrate_layouts'] = []
        # substrate tree[0] = user label
        # substrate tree[1] = ref des
        # substrate tree[2] = ypad
        # substrate tree[3] = layout name
        for row in gui_dict['substrate_tree']['value']['table_list']:
            if row[0] == "":
                args['substrate_labels'].append(row[1])
            else:
                args['substrate_labels'].append(row[0])
            args['substrate_layouts'].append(row[3])

        args['iv_subs_names'] = self.bitmask_to_some_lists(args['iv_devs'])['subs_names']
        args['iv_subs_dev_nums'] = self.bitmask_to_some_lists(args['iv_devs'])['sub_dev_nums']
        args['iv_selections'] = self.bitmask_to_some_lists(args['iv_devs'])['selections']
        args['iv_subs_labels'] = self.bitmask_to_some_lists(args['iv_devs'])['user_labels']
        for i, lab in enumerate(args['iv_subs_labels']):  # change empty labels to ref des
            if lab == '':
                args['iv_subs_labels'][i] = args['iv_subs_names'][i]

        args['eqe_subs_names'] = self.bitmask_to_some_lists(args['eqe_devs'])['subs_names']
        args['eqe_subs_dev_nums'] = self.bitmask_to_some_lists(args['eqe_devs'])['sub_dev_nums']
        args['eqe_selections'] = self.bitmask_to_some_lists(args['eqe_devs'])['selections']
        args['eqe_subs_labels'] = self.bitmask_to_some_lists(args['eqe_devs'])['user_labels']
        for i, lab in enumerate(args['eqe_subs_labels']):  # change empty labels to ref des
            if lab == '':
                args['eqe_subs_labels'][i] = args['eqe_subs_names'][i]
        
        args['subs_names'] = self.substrate_designators
        #if int(args['eqe_devs'], 16) == 0:
        #    args['eqe_devs'] = None

        #if int(args['iv_devs'], 16) == 0:
        #    args['iv_devs'] = None

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

    def on_cal_psu_button(self, button):
        """Measure EQE calibration photodiode."""
        """Send run info to experiment orchestrator via MQTT."""
        if (self.move_warning() == Gtk.ResponseType.OK):
            self.b.get_object("run_but").set_sensitive(False)  # prevent run
            lg.info(f"Starting bias light LED calibration")

            msg = {"cmd":"run", "args": self.gui_to_args(self.harvest_gui_data()), "config": self.config}
            pic_msg = pickle.dumps(msg, protocol=pickle.HIGHEST_PROTOCOL)

            self.mqttc.publish("measurement/calibrate_psu", pic_msg, qos=2).wait_for_publish()

    # called on right click to log before the menu is drawn
    def on_log_pre_popup(self, text_view, menu):
        sep = Gtk.SeparatorMenuItem()
        sep.set_visible(True)
        menu.prepend(sep)
        clmi = Gtk.MenuItem()
        clmi.set_label('Clear Log')
        clmi.set_visible(True)
        clmi.connect('activate',self.clear_log)
        menu.prepend(clmi)

    def clear_log(self, widget):
        self.logTB.set_text("")

    def on_smart_mode_activate(self, button):
        self.update_gui()
    
    # handle changes in stack
    def on_stack_change(self, stack, child):
        active_title = stack.child_get_property(stack.get_visible_child(),'title')
        if active_title == 'Live Data':
            self.load_live_data_webviews(load=True)
        else:
            self.load_live_data_webviews(load=False)
        
        if active_title == 'Custom View':
            self.load_custom_webview(load=True)
        else:
            self.load_custom_webview(load=False)
        
        self.b.get_object("pane").set_position(0)  # move the pane handle to the top every stack change

    # pause/unpause plots
    def on_plotter_switch(self, switch, state):
        pic_msg = pickle.dumps(not state, protocol=pickle.HIGHEST_PROTOCOL)
        self.mqttc.publish("plotter/pause", pic_msg, qos=2).wait_for_publish()

    # invert voltage plots switch
    def on_voltage_switch(self, switch, state):
        pic_msg = pickle.dumps(state, protocol=pickle.HIGHEST_PROTOCOL)
        self.mqttc.publish("plotter/invert_voltage", pic_msg, qos=2).wait_for_publish()

    # invert current plots switch
    def on_current_switch(self, switch, state):
        pic_msg = pickle.dumps(state, protocol=pickle.HIGHEST_PROTOCOL)
        self.mqttc.publish("plotter/invert_current", pic_msg, qos=2).wait_for_publish()

    # reads various gui item states and sets others accordingly
    # def needs to be called after loading a gui state file
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
        self.update_measure_count(True)
        self.update_measure_count(False)


if __name__ == "__main__":
    app = App()
    app.run(sys.argv)
