#!/usr/bin/env python

import pathlib

# import time
# import signal # to handle key kill
import logging
import systemd.journal

# import logging.handlers
import gi
import sys
import time
import math
import humanize
import datetime as dt
import paho.mqtt.client as mqtt
import configparser
import json

# os.environ["DEBUSSY"] = "1"

# gi.require_version('WebKit2', '4.0')
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import GLib, Gio, Gtk, Gdk, Pango

# Gdk.set_allowed_backends('broadway')
# from gi.repository.WebKit2 import WebView, Settings
# gi.require_version('WebKit2', '4.0')
# from gi.repository import WebKit2 as Webkit


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
    ids = [
        "eqe_in",
        "eqe_int",
        "eqedevbias",
        "firstEnd",
        "firstStart",
        "gblc",
        "image1",
        "iscdwell",
        "mpptTime",
        "nmStart",
        "nmStep",
        "nmStop",
        "nmStop1",
        "nmWidth",
        "nplc",
        "rblc",
        "secondEnd",
        "secondStart",
        "sweepDelay",
        "sweepSteps",
        "vocdwell",
        "run_name_prefix",
        "iv_devs",
        "autoiv",
        "eqe_devs",
    ]

    def __init__(self, config=None, *args, **kwargs):
        """Constructor.

        Parameters
        ----------
        MQTTHOST : str
            MQTT broker IP address or hostname.
        config : config parser object
            Config parser object created from config file.
        """
        super().__init__(
            *args,
            application_id="net.christoforo.control-ui",
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
            **kwargs,
        )

        # config parser object
        self.config = config

        # get dimentions of substrate array to generate designators
        number_list = [int(x) for x in self.congig["substrates"]["number"].split(",")]
        self.substrate_designators = self._generate_substrate_designators(number_list)
        self.numSubstrates = len(self.substrate_designators)

        self.main_win = None
        self.b = None
        self.numPix = 6
        self.approx_seconds_per_iv = 50

        galde_ui_xml_file_name = "ui.glade"
        gui_file = pathlib.Path(galde_ui_xml_file_name)
        if gui_file.is_file():
            self.galde_ui_xml_file = str(gui_file)
        elif ("python" / gui_file).is_file():  # for debugging
            self.galde_ui_xml_file = str("python" / gui_file)
        else:
            raise (ValueError("Can't find glade file!"))

        self.add_main_option(
            "test",
            ord("t"),
            GLib.OptionFlags.NONE,
            GLib.OptionArg.NONE,
            "Command line test",
            None,
        )

        # connect to mqtt broker
        self.MQTTHOST = self.config["network"]["MQTTHOST"]
        self.mqttc = mqtt.Client()
        self.mqttc.connect(self.MQTTHOST)
        self.mqttc.loop_start()

    def _generate_substrate_designators(self, number_list):
        """Generate a list of substrate designators.

        Parameters
        ----------
        number_list : list
            List of numbers of substrates along each available axis. Length must be
            1 or 2.
        """
        rs = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"[:number_list[0]]

        try:
            cs = range(number_list[1])
        except IndexError:
            # if number of columns not given, must be 1
            cs = range(1)

        subdes = [f"{m}{n + 1}" for m in rs for n in cs]

        return subdes


    def do_startup(self):
        lg.debug("Starting up app")
        Gtk.Application.do_startup(self)

        self.b = Gtk.Builder()
        self.b.add_from_file(self.galde_ui_xml_file)

        self.label_tree, self.label_store = self.setup_label_tree()
        self.dev_tree, self.dev_store = self.setup_picker_tree()

        max_devices = self.numPix * self.numSubstrates
        address_string_length = math.ceil(max_devices / 4)
        selection_box_length = 4 + address_string_length + 3
        default_on = "0x" + "F" * address_string_length
        default_off = "0x" + "0" * address_string_length
        self.iv_dev_box = self.b.get_object("iv_devs")
        self.eqe_dev_box = self.b.get_object("eqe_devs")
        self.def_fmt_str = f"0{address_string_length}X"

        # TODO: do this in a non-obsolete way (probably w/ css)
        fontdesc = Pango.FontDescription("monospace")

        self.iv_dev_box.modify_font(fontdesc)
        self.iv_dev_box.set_text(default_on)
        self.last_valid_devs = default_on
        self.iv_dev_box.set_width_chars(selection_box_length)
        self.iv_dev_box.set_icon_from_icon_name(0, "emblem-default")
        self.eqe_dev_box.modify_font(fontdesc)
        self.eqe_dev_box.set_text(default_off)
        self.eqe_dev_box.set_width_chars(selection_box_length)

        self.po = self.b.get_object("iv_pop")
        self.po.set_position(Gtk.PositionType.BOTTOM)
        self.tick()
        self.ticker = self.timeout_id = GLib.timeout_add_seconds(1, self.tick, None)
        self.b.connect_signals(self)  # maps all ui callbacks to functions here

    def setup_picker_tree(self):
        deviceTree = self.b.get_object("devTV")
        devStore = Gtk.TreeStore(str, bool, bool)
        checked = True
        inconsistent = False
        # fill in the model
        for i in range(self.numSubstrates):
            if self.label_store[i][0] == "":
                label = self.label_store[i][1]
            else:
                label = self.label_store[i][0]
            piter = devStore.append(None, [label, checked, inconsistent])
            j = 1
            while j <= self.numPix:
                devStore.append(piter, [f"Device {j}", checked, inconsistent])
                j += 1

        deviceTree.set_model(devStore)
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

        return (deviceTree, devStore)

    # callback function for select/deselect device/substrate
    def dev_toggle(self, widget, path):
        path_split = path.split(":")
        # the boolean value of the selected row
        current_value = self.dev_store[path][1]
        # change the boolean value of the selected row in the model
        self.dev_store[path][1] = not current_value
        # new current value!
        current_value = not current_value
        # if length of the path is 1 (that is, if we are selecting a substrate)
        if len(path_split) == 1:
            # get the iter associated with the path
            piter = self.dev_store.get_iter(path)
            self.dev_store[piter][2] = False
            # get the iter associated with its first child
            citer = self.dev_store.iter_children(piter)
            # while there are children, change the state of their boolean value
            # to the value of the substrate
            while citer is not None:
                self.dev_store[citer][1] = current_value
                citer = self.dev_store.iter_next(citer)
        # if the length of the path is not 1 (that is, if we are selecting a
        # device)
        elif len(path_split) != 1:
            # get the first child of the parent of the substrate (device 1)
            citer = self.dev_store.get_iter(path)
            piter = self.dev_store.iter_parent(citer)
            citer = self.dev_store.iter_children(piter)
            # check if all the children are selected
            num_selected = 0
            while citer is not None:
                if self.dev_store[citer][1] is True:
                    num_selected = num_selected + 1
                citer = self.dev_store.iter_next(citer)
            # if they do, the device as well is selected; otherwise it is not
            if num_selected == self.numPix:
                self.dev_store[piter][2] = False
                self.dev_store[piter][1] = True
            elif num_selected == 0:
                self.dev_store[piter][2] = False
                self.dev_store[piter][1] = False
            else:
                self.dev_store[piter][2] = True

        # iterate through everything and build up the result
        siter = self.dev_store.get_iter("0")  # substrate iterator
        selection_bitmask = 0
        bit_location = 0
        while siter is not None:
            diter = self.dev_store.iter_children(siter)  # device iterator
            while diter is not None:
                if self.dev_store[diter][1] is True:
                    selection_bitmask = selection_bitmask + (1 << bit_location)
                bit_location = bit_location + 1
                diter = self.dev_store.iter_next(diter)  # advance to the next device
            siter = self.dev_store.iter_next(siter)  # advance to the next substrate

        self.iv_dev_box.set_text(f"0x{selection_bitmask:{self.def_fmt_str}}")

    def on_iv_devs_changed(self, editable, user_data=None):
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
            self.last_valid_devs = text_is
            self.iv_dev_box.set_icon_from_icon_name(0, "emblem-default")
            self.iv_measure_note(selection_bitmask)
        else:
            self.iv_dev_box.set_icon_from_icon_name(0, "dialog-error")

    def on_iv_devs_focus_out_event(self, event, user_data=None):
        if self.iv_dev_box.get_icon_name(0) != "emblem-default":
            self.iv_dev_box.set_text(self.last_valid_devs)
        else:
            self.iv_dev_box.set_text(self.last_valid_devs)

    # device bitmask string reformatter
    def on_iv_devs_activate(self, entry, user_data=None):
        text_is = entry.get_text()
        try:
            selection_bitmask = int(text_is, 16)
            should_be = f"0x{selection_bitmask:{self.def_fmt_str}}"
            entry.set_text(should_be)
        except:
            entry.set_text(self.last_valid_devs)

    # log message printer for device selection change
    def iv_measure_note(self, selection_bitmask):
        num_selected = sum([c == "1" for c in bin(selection_bitmask)])
        lg.info(f"{num_selected} devices selected for I-V measurement")
        duration_string = humanize.naturaldelta(
            dt.timedelta(seconds=self.approx_seconds_per_iv * num_selected)
        )
        lg.info(f"Which might take {duration_string} to measure")

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
            self.dev_store[path][0] = self.label_store[path][1]
        else:
            self.dev_store[path][0] = self.label_store[path][0]

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
            if h.get_name() == "ui":
                lg.removeHandler(h)
                lg.debug("Shutting down")
        Gtk.Application.do_shutdown(self)

    def on_runCodeButton_clicked(self, button):
        lg.debug("Hello World!")

    def on_iv_devs_icon_release(self, icon, b, user_data=None):
        sw = self.dev_tree.get_parent()  # scroll window
        sw.set_min_content_height((self.numSubstrates + 1) * 25)
        if self.iv_dev_box.get_icon_name(0) != "emblem-default":
            self.iv_dev_box.set_text(self.last_valid_devs)
        text_is = self.iv_dev_box.get_text()
        selection_bitmask = int(text_is, 16)

        bin_mask = bin(selection_bitmask)[2::]
        bin_mask_rev = bin_mask[::-1]

        # iterate through everything and build up the result
        siter = self.dev_store.get_iter("0")  # substrate iterator
        bit_location = 0
        while siter is not None:
            num_enabled = (
                0  # keeps track of number of enabled devices on this substrate
            )
            diter = self.dev_store.iter_children(siter)  # device iterator
            while diter is not None:
                if (bit_location + 1 <= len(bin_mask_rev)) and (
                    bin_mask_rev[bit_location] == "1"
                ):
                    self.dev_store[diter][1] = True
                    num_enabled = num_enabled + 1
                else:
                    self.dev_store[diter][1] = False
                bit_location = bit_location + 1
                diter = self.dev_store.iter_next(diter)  # advance to the next device
            if num_enabled == 0:
                self.dev_store[siter][1] = False  # set substrate off
            elif num_enabled == self.numPix:
                self.dev_store[siter][1] = True  # set substrate on
            else:
                self.dev_store[siter][2] = True  # set substrate inconsistant
            siter = self.dev_store.iter_next(siter)  # advance to the next substrate

        self.po.show_all()
        # lg.debug(sw.get_allocated_height())
        return True

    def save(self, path):
        """Save current state of widget entries to a file.

        Parameters
        ----------
        path : path or str
            Save path.
        """
        data = {}
        for id_str in ids:
            data[id_str] = self.b.get_object(id_str).get_text()

        with open(path, "w") as f:
            json.dump(data, f)

    def open(self, path):
        """Populate widget entries from data saved in a file.

        Parameters
        ----------
        path : path or str
            Path to open.
        """
        with open(path, "r") as f:
            data = json.load(f)

        for key, value in data:
            self.b.get_object(key).set_text(value)

    def start_run(self):
        """Send run info to experiment orchestrator via MQTT."""
        save_folder = pathlib.Path(self.config["paths"]["save_folder"])
        run_name = self.b.get_object("run_name").get_text()
        destination = str(save_folder.joinpath(run_name))

        iv_pixel_address = self.b.get_object("iv_devs").get_text()
        eqe_pixel_address = self.b.get_object("eqe_devs").get_text()
        steadystate_v = 0
        steadystate_i = 0
        v_t = float(self.b.get_object("vocdwell").get_text())
        i_t = float(self.b.get_object("iscdwell").get_text())
        mppt_t = float(self.b.get_object("mpptTime").get_text())
        mppt_params =
        layout_index =
        light_recipe = 
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
            float(self.config["psu"]["ch3_voltage"])
        ]
        psu_is = [
            float(self.b.get_object("gblc").get_text()),
            float(self.b.get_object("rblc").get_text()),
            0
        ]
        eqe_smu_v = float(self.b.get_object("eqedevbias").get_text())
        eqe_ref_meas_path =
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
            "motion_address" : motion_address,
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
            "eqe_grating_change_wls": eqe_filter_change_wls,
        }

        # add optional parameters if required
        if self.b.get_object("autoiv").get_text() == "False":
            scan_start_override_1 = float(self.b.get_object("firstStart").get_text())
            scan_end_override_1 = float(self.b.get_object("firstEnd").get_text())
            scan_start_override_2 = float(self.b.get_object("secondStart").get_text())
            scan_end_override_2 = float(self.b.get_object("secondEnd").get_text())
            settings["scan_start_override_1"] = scan_start_override_1
            settings["scan_end_override_1"] = scan_end_override_1
            settings["scan_start_override_2"] = scan_start_override_2
            settings["scan_end_override_2"] = scan_end_override_2

        # send settings dict over mqtt
        payload = json.dumps(settings)
        self.mqttc.publish("gui", payload, qos=2).wait_for_publish()

    def calibrate_eqe(self):
        """Measure EQE calibration photodiode."""
        save_folder = pathlib.Path(self.config["paths"]["save_folder"])
        run_name = self.b.get_object("run_name").get_text()
        destination = str(save_folder.joinpath(run_name))

        # Arbitrary dummy bitmask containing a single pixel.
        eqe_pixel_address = "0x000000000001"

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
            float(self.config["psu"]["ch3_voltage"])
        ]
        psu_is = [
            float(self.b.get_object("gblc").get_text()),
            float(self.b.get_object("rblc").get_text()),
            0
        ]
        eqe_smu_v = float(self.b.get_object("eqedevbias").get_text())
        eqe_ref_meas_path =
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
            "eqe_grating_change_wls": eqe_filter_change_wls,
        }

        # send settings dict over mqtt
        payload = json.dumps(settings)
        self.mqttc.publish("gui", payload, qos=2).wait_for_publish()

    def calibrate_psu(self):
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
            "calibrate_psu_ch": float(self.b.get_object("psu_cal_ch").get_text()),
        }

        # send settings dict over mqtt
        payload = json.dumps(settings)
        self.mqttc.publish("gui", payload, qos=2).wait_for_publish()

    def _get_layouts_str(self):
        """Read and format layouts from config file.

        This function strips the comments from layout sections of the config file so
        they can be sent more efficiently over MQTT.
        """
        layout_names = self.config["substrates"]["layout_names"].split(",")

        layouts_str = f""
        for name in layout_names:
            layouts_str += f"""
            [{name}]
            positions={self.config[name]["positions"]}
            areas={self.config[name]["areas"]}
            """

        return layouts_str


if __name__ == "__main__":
    # load info from config file
    config = configparser.ConfigParser(interpolation=configparser.ExtendedInterpolation())
    config.read("config.ini")

    app = App()
    app.run(config, sys.argv)
