#!/usr/bin/env python

import pathlib

# import time
# import signal # to handle key kill
import logging
import systemd.journal
import pprint

# for merging dictionaries
from boltons.iterutils import default_enter
from boltons.iterutils import default_visit
from boltons.iterutils import get_path
from boltons.iterutils import remap

# for drawing layouts
import drawSvg as draw

import gi
import sys
import os
import time
import math
import humanize
import datetime as dt
import paho.mqtt.client as mqtt
import pickle
import pandas as pd
import numpy as np

import yaml

import re

# os.environ["DEBUSSY"] = "1"

gi.require_version("WebKit2", "4.0")
gi.require_version("Gtk", "3.0")
gi.require_version('Rsvg', '2.0')
from gi.repository import GLib, Gio, Gtk, Gdk, Rsvg

# Gdk.set_allowed_backends('broadway')  # for gui over web
from gi.repository.WebKit2 import WebView, Settings

# needed to filter out a stupid bug-warning in gio
# https://bugzilla.gnome.org/show_bug.cgi?id=708676
# that shows up on input validation
import warnings
warnings.filterwarnings('ignore', '.*g_value_get_int.*G_VALUE_HOLDS_INT.*', Warning)

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
        self.array_drawing_handle = None
        self.layout_drawing_handle = None

        # allow configuration file location to be specified by command line argument
        self.add_main_option(
            "config",
            ord("c"),
            GLib.OptionFlags.NONE,
            GLib.OptionArg.FILENAME,
            "Configuration file",
            None,
        )

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

    def do_activate(self):
        lg.debug("Activating app")

        # We only allow a single window and raise any existing ones
        if self.main_win is None:
            # Windows are associated with the application
            # when the last one is closed the application shuts down

            self.logTB = self.b.get_object("tbLog")  # log text buffer
            self.ltv = self.b.get_object("ltv")  # log text view
            self.log_win_adj = self.b.get_object("vert_log_win_scroll_adj")
            self.array_pic = self.b.get_object("array_overview")
            self.array_pic.connect("draw", self.on_array_pic_draw)
            self.subs_pic = self.b.get_object("substrate_pic")
            self.subs_pic.connect("draw", self.on_subs_pic_draw)


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
                #with open(self.config_file, "r") as f:
                #    for line in f:
                #        lg.debug(line.rstrip())
                with open(self.config_file, "r") as f:
                    base_config = yaml.load(f, Loader=yaml.FullLoader)
            except:
                lg.error("Unexpected error parsing config file.")
                lg.error(sys.exc_info()[0])
                raise

            # now let's load auxiliary config files and merge the configs
            aux_configs = []
            aux_config_dir_exists = False
            try:
                ip = base_config['meta']['include_path']
                if pathlib.Path(ip).is_absolute():
                    include_path = pathlib.Path(ip)
                else:
                    include_path = pathlib.Path.home() / ip
                if include_path.exists() and include_path.is_dir():
                    aux_config_dir_exists = True
            except:
                pass
            if aux_config_dir_exists == True:
                for pth in include_path.iterdir():
                    try:
                        with open(pth, "r") as f:
                            new_config = yaml.load(f, Loader=yaml.FullLoader)
                            lg.info(f"Using auxillary config file: {pth}")
                        aux_configs.append(new_config)
                    except:
                        pass

            self.config = merge_dicts([base_config] + aux_configs)

            pp = pprint.PrettyPrinter(compact=True, width=140, sort_dicts=False)
            lg.debug(pp.pformat(self.config))

            try:
                self.b.get_object("user_name_frame").set_visible(self.config['UI']['show_user_box'])
            except:
                pass

            try:
                self.b.get_object("custom_wv").set_visible(self.config['UI']['show_custom_tab'])
            except:
                pass

            # get dimentions of substrate array to generate designators
            self.counts = self.config["substrates"]["number"]
            self.spacings = self.config["substrates"]["spacing"]
            try:
                polarities = self.config["stage"]["flip_axis_polarity"]
            except:
                polarities = [False]*len(self.counts)
            try:
                lab_flips = self.config["substrates"]["flip_labels"]
            except:
                lab_flips = [False]*len(self.counts)
            label_grid, position_grid = self.make_meshgrids(self.counts, self.spacings, polarities, lab_flips)

            # HINT: visualize 1 and 2d meshgrids by printing transposed like this
            # for comparison to the physical layout to see if it's right:
            # print(label_meshgrid.T)

            labels = self.grid_to_list(label_grid)
            pos_list = self.grid_to_list(position_grid)
            self.substrate_locations = dict(zip(labels, pos_list))
            self.substrate_designators = sorted(labels)
            ns = len(self.substrate_designators)

            # are we using a stage controller here?
            try:
                self.enable_stage = self.config["stage"]["enabled"] == True
            except:
                self.enable_stage = False
            
            # handle custom locations and stage stuff
            if self.enable_stage == True:
                pl = self.b.get_object("places_list")  # a tree model
                self.custom_coords = []
                if "experiment_positions" in self.config["stage"]:
                    for key, val in self.config["stage"]["experiment_positions"].items():
                        pl.append(['EXPERIMENT -- ' + key])
                        self.custom_coords.append(val)

                if "custom_positions" in self.config["stage"]:
                    for key, val in self.config["stage"]["custom_positions"].items():
                        pl.append([key])
                        self.custom_coords.append(val)

                if len(self.custom_coords) == 0:
                    self.b.get_object("places_combo").set_visible(False)
                    self.b.get_object("places_label").set_visible(False)

                # stage specific stuff
                stage_uri_split = self.config["stage"]["uri"].split('://')
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
                self.num_axes = 0
                self.b.get_object("stage_util").set_visible(False)
                self.b.get_object("home_util").set_visible(False)

            # are we using a lockin here?
            try:
                self.enable_lia = self.config["lia"]["enabled"] == True
            except:
                self.enable_lia = False

            # are we using a monochromator here?
            try:
                self.enable_mono = self.config["monochromator"]["enabled"] == True
            except:
                self.enable_mono = False

            # are we using a SMU here?
            try:
                self.enable_smu = self.config["smu"]["enabled"] == True
            except:
                self.enable_smu = False

            # are we using a solar sim here?
            try:
                self.enable_solarsim = self.config["solarsim"]["enabled"] == True
            except:
                self.enable_solarsim = False

            # are we using a bias light PSU here?
            try:
                self.enable_psu = self.config["psu"]["enabled"] == True
            except:
                self.enable_psu = False
            
            # enable/disable logic
            self.enable_eqe = False
            if (self.enable_mono == True) and (self.enable_lia == True) and (self.enable_smu == True):
                self.enable_eqe = True
            
            self.enable_iv = False
            if (self.enable_smu == True):
                self.enable_iv = True

            # hide GUI elements that don't match what were configured to use
            if self.enable_eqe == False:
                self.b.get_object("eqe_frame").set_visible(False)
                self.b.get_object("eqe_util").set_visible(False)
                self.b.get_object("eqe_wv").set_visible(False)
            else:
                if self.enable_psu == False:
                    self.b.get_object("psu_frame").set_visible(False)

            if self.enable_iv == False:
                self.b.get_object("iv_frame").set_visible(False)
                self.b.get_object("vt_wv").set_visible(False)
                self.b.get_object("iv_wv").set_visible(False)
                self.b.get_object("mppt_wv").set_visible(False)
                self.b.get_object("jt_wv").set_visible(False)
            else:
                if self.enable_solarsim == False:
                    self.b.get_object("ss_box").set_visible(False)
                    self.b.get_object("ill_box").set_visible(False)

            if (self.enable_smu == False) or (self.enable_psu == False):
                self.b.get_object("bias_light_util").set_visible(False)

            if (self.enable_iv == False) or (self.enable_eqe == False):
                self.b.get_object("eqe_relay_util").set_visible(False)

            if (self.enable_mono == False):
                self.b.get_object("mono_util").set_visible(False)

            # do layout things
            layouts = []  # list of enabled layouts
            npix = []  # number of pixels for each layout
            areas = []  # area list for each layout
            pads = []  # pin pad list for each layout
            self.layout_drawings = {}  # holds our pictures of the layouts
            self.rotated_layout_drawings = {}  # holds our pictures of the layouts
            if 'substrates' in self.config:
                if 'layouts' in self.config['substrates']:
                    for layout_name, val in self.config['substrates']['layouts'].items():
                        if 'enabled' in val:
                            if val['enabled'] == True:
                                layouts.append(layout_name)
                                pads.append(val['pads'])
                                npix.append(len(val['pads']))
                                areas.append(val['areas'])
                                d, dr = self.draw_layout(val['pads'], val['areas'], val['locations'], val['shapes'], val['size'], self.spacings, layout_name)
                                self.layout_drawings[layout_name] = d
                                self.rotated_layout_drawings[layout_name] = dr
            self.layouts = layouts
            lnd = 0  # default starting layout number

            # slot configuration stuff
            self.slot_config_tv = self.b.get_object("substrate_tree")
            self.setup_slot_config_tv(self.slot_config_tv, layouts)
            self.slot_config_store = Gtk.ListStore(str, str, str)  # ref des, user label, layout name
            self.slot_config_store.variables = []
            self.slot_config_tv.set_model(self.slot_config_store)
            self.fill_slot_config_store(self.slot_config_store, self.substrate_designators, ['']*ns, [layouts[lnd]]*ns)
            #self.slot_config_store.connect('row-changed', self.on_slot_store_change)
            self.add_variable('Variable')

            # device selection stuff
            self.device_select_tv = self.b.get_object("device_tree")
            self.setup_device_select_tv(self.device_select_tv)
            # these treestores contain the info on which devices are selected for measurement
            # [str, bool, bool, str, bool] is for [label, checked, inconsistent, area, check visible]
            self.iv_store = Gtk.TreeStore(str, bool, bool, str, bool)
            self.iv_store.set_name('IV Device Store')
            self.eqe_store = Gtk.TreeStore(str, bool, bool, str, bool)
            self.eqe_store.set_name('EQE Device Store')
            self.fill_device_select_store(self.iv_store,  [[True]*npix[lnd]]*ns,  self.substrate_designators, ['']*ns, [layouts[lnd]]*ns, [areas[lnd]]*ns, [pads[lnd]]*ns)
            self.fill_device_select_store(self.eqe_store, [[False]*npix[lnd]]*ns, self.substrate_designators, ['']*ns, [layouts[lnd]]*ns, [areas[lnd]]*ns, [pads[lnd]]*ns)

            abs_max_devices = max(npix) * ns
            max_address_string_length = math.ceil(abs_max_devices / 4)
            selection_box_length = max_address_string_length + 2

            self.iv_dev_box = self.b.get_object("iv_devs")
            sc = self.iv_dev_box.get_style_context()
            sc.add_class(Gtk.STYLE_CLASS_MONOSPACE)
            self.iv_dev_box.set_width_chars(selection_box_length)
            self.iv_dev_box.connect('changed', self.update_measure_count)

            self.eqe_dev_box = self.b.get_object("eqe_devs")
            sc = self.eqe_dev_box.get_style_context()
            sc.add_class(Gtk.STYLE_CLASS_MONOSPACE)
            self.eqe_dev_box.set_width_chars(selection_box_length)
            self.eqe_dev_box.connect('changed', self.update_measure_count)

            self.do_dev_store_update_tasks(self.iv_store)
            self.do_dev_store_update_tasks(self.eqe_store)

            # the device picker popover
            self.po = self.b.get_object("picker_po")
            self.po.set_position(Gtk.PositionType.BOTTOM)

            # the layout popover
            self.lopo = self.b.get_object("layout_po")
            self.lopo.set_position(Gtk.PositionType.RIGHT)
            self.lopo.set_relative_to(self.slot_config_tv)
            #self.lopo.add(Gtk.Image())
            #self.lopo.add(Gtk.Label(label=layouts[0]))

            # for approximating runtimes
            #self.approx_seconds_per_iv = 50
            #self.approx_seconds_per_eqe = 150

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

            # start MQTT client
            self._start_mqtt()

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
                if 'invert_voltage' in self.config['UI']:
                    sw = self.b.get_object('inv_v_switch')
                    sw.set_active(self.config['plots']['invert_voltage'])
                if 'invert_current' in self.config['UI']:
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

            # for doing tasks when the user makes a stack change
            ms = self.b.get_object('mainStack')
            ms.connect("notify::visible-child", self.on_stack_change)

            # for handling global accelerator key combos
            ag = self.b.get_object('global_keystrokes')
            # setup debug key combo
            ag.connect(Gdk.keyval_from_name('D'), Gdk.ModifierType.CONTROL_MASK, 0, self.do_debug_tasks)

            # disallow bad input for some boxes
            rnp = self.b.get_object("run_name_prefix")
            rnp.connect('insert-text', self.only_alnum)
            un = self.b.get_object("user_name")
            un.connect('insert-text', self.only_alnum)

            # do one tick now and then start the backround tick launcher
            self.tick()
            self.ticker_id = GLib.timeout_add_seconds(1, self.tick, None)
            self.b.connect_signals(self)  # maps all ui callbacks to functions here

            self.main_win = self.b.get_object("mainWindow")
            self.main_win.set_application(self)

        self.main_win.present()

    # take a mesh grid and turn it into a list
    def grid_to_list(self, grid):
        lis = []
        fl = grid.flat
        for x in fl:
            lis.append(x)
        return lis

    # use some variables from the config file to generate
    # arrays of labels and positions for each substrate slot in the setup
    def make_meshgrids(self, counts, spacings, polarities, lab_flips):
        d = len(counts)  # number of dimensions
        # the labeling start character is kinda wonky so we can handle it here based on d
        if d == 1:
            label_starts = ['A']
        elif d == 2:
            label_starts = ['1','A']
        else:
            label_starts = ['a','1','A']
        ranges = [np.array(range(x)) for x in counts]
        label_ranges =    [r+ord(label_starts[i]) for i,r in enumerate(ranges)]
        location_ranges = [r*spacings[i] for i,r in enumerate(ranges)]
        # center the location ranges on zero and flip them if the axis polarity value is 1
        location_ranges = [(r-r.max()/2)*(-2*polarities[i]+1) for i,r in enumerate(location_ranges)]  

        # make some empty grids of the right shapes and types
        label_meshgrid = np.empty(list(counts), dtype=np.dtype(f'U{d}'))
        pos_meshgrid = np.empty(list(counts), dtype=object)
        #labels = []
        #positions = []

        # populate the grids
        for idx, x in np.ndenumerate(label_meshgrid):
            label = ''
            pos = []
            for j,i in enumerate(idx):
                label += chr(label_ranges[j][i])
                pos.append(location_ranges[j][i])
            label_meshgrid[idx] = label
            pos_meshgrid[idx] = pos
            #labels.append(label)
            #positions.append(pos)

        # flip the labels if the config file says so
        for i, t in enumerate(lab_flips):
            if t == True:
                label_meshgrid = np.flip(label_meshgrid, i)

        return label_meshgrid, pos_meshgrid

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

    def update_measure_count(self, entry):
        parent_frame = entry.get_parent().get_parent()
        text_is = entry.get_text()
        selection_bitmask = int(text_is, 16)
        num_selected = sum([c == "1" for c in bin(selection_bitmask)])
        txt = f'Device Selection Bitmask ({num_selected} selected)'
        parent_frame.set_label(txt)

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

    # this configures a treeview object for use in selecting/unselecing devices.
    def setup_device_select_tv(self, tree_view):
        if len(tree_view.get_columns()) == 0:
            renderDesignator = Gtk.CellRendererText()
            # the first column is created
            designator = Gtk.TreeViewColumn("Substrate/Device", renderDesignator, text=0)
            tree_view.append_column(designator)

        if len(tree_view.get_columns()) == 1:
            renderDesignator = Gtk.CellRendererText()
            # the first column is created
            designator = Gtk.TreeViewColumn("Area/Layout", renderDesignator, text=3)
            tree_view.append_column(designator)

        # the cellrenderer for the second column - boolean rendered as a toggle
        if len(tree_view.get_columns()) == 2:
            renderCheck = Gtk.CellRendererToggle()
            # the second column is created
            colCheck = Gtk.TreeViewColumn("Measure?", renderCheck, active=1, inconsistent=2, visible=4)
            # colCheck.set_clickable(True)
            # colCheck.connect("clicked", self.dev_col_click)
            tree_view.append_column(colCheck)
            # connect the cellrenderertoggle with a callback function
            renderCheck.connect("toggled", self.on_dev_toggle, colCheck)

        # this handles left/right arrow buttons for expanding and collapsing rows
        tree_view.connect("key-release-event", self.handle_dev_key)

    # populates a device selection store with the default startup values
    # checkmarks is a list of lists of booleans
    # len(checkmarks) is the number of substrates
    # and the lengths of the inner lists are the number of pixels on each substrate
    def fill_device_select_store(self, store, checkmarks, substrate_designators, labels, layouts, areas, pads):
        store.clear()
        # compute the checkbox state for the top level
        any_check = False
        all_check = True
        for subs_bool in checkmarks:
            if all(subs_bool):
                any_check = True
            else:
                all_check = False
                if any(subs_bool):
                    any_check = True
        if all_check == True:
            # [str, bool, bool, str, bool] is for [label, checked, inconsistent, area, check visible]
            checked = True
            inconsistent = False
        else:
            checked = False
            if any_check == True:
                inconsistent = True
            else:
                inconsistent = False
        total_devices = 0
        for subs_areas in areas:
            total_devices += len(subs_areas)
        if total_devices == 0:
            top_vis = False
        else:
            top_vis = True
        top = store.append(None, ['All', checked, inconsistent, '', top_vis])

        # now we can fill in the model
        for i, subs_checks in enumerate(checkmarks):  # iterate through substrates
            this_label = substrate_designators[i]
            subs_areas = areas[i]
            subs_pads = pads[i]
            if labels[i] != "":
                this_label += f": {labels[i]}"
            if all(subs_checks):
                checked = True
                inconsistent = False
            else:
                checked = False
                if any(subs_checks):
                    inconsistent = True
                else:
                    inconsistent = False
            # append a substrate header row
            piter = store.append(top, [this_label, checked, inconsistent, layouts[i], len(subs_areas) != 0])
            for checked, area, pad in zip(subs_checks, subs_areas, subs_pads):
                # append a device row
                store.append(piter, [f"Device {pad}", checked, False, str(area), True])

    # sets up the columns the slot configuration treeview will show us
    def setup_slot_config_tv(self, tree_view, layouts):
        # clear the cols
        for col in tree_view.get_columns():
            tree_view.remove_column(col)

        # the refrence designator column
        ref_des_cell = Gtk.CellRendererText()
        ref_des_col = Gtk.TreeViewColumn("Slot", ref_des_cell, text=0)
        tree_view.append_column(ref_des_col)

        # the editable substrate label column
        # only append the col if it's not already there
        # fixes double col on file load
        store_col = 1
        label_cell = Gtk.CellRendererText()
        label_cell.set_property("editable", True)
        label_cell.connect("edited", self.on_slot_cell_edit, store_col)
        labels_col = Gtk.TreeViewColumn("Label", label_cell, text=store_col)
        tree_view.append_column(labels_col)

        # the layout dropdown selection column
        store_col = 2
        # this allows me to find the combo box object
        tree_view.connect('set-focus-child', self.on_layout_combo_focus)
        layout_cell = Gtk.CellRendererCombo()
        layouts_store = Gtk.ListStore(str)  # holds the layout options
        for layout in layouts:
            layouts_store.append([layout])
        layout_cell.set_property("editable", True)
        layout_cell.set_property("model", layouts_store)
        layout_cell.set_property("has-entry", False)
        layout_cell.set_property("text-column", 0)
        layout_cell.connect("changed", self.on_layout_combo_changed)
        layout_col = Gtk.TreeViewColumn("Layout", layout_cell, text=store_col)
        tree_view.append_column(layout_col)

    def on_col_header_click(self, col, store_col):
        d = Gtk.MessageDialog(parent=self.main_win, buttons=Gtk.ButtonsType.CANCEL)
        d.set_property('text', '<b>Variable Management</b>')
        d.set_property('message-type', Gtk.MessageType.QUESTION)
        d.set_property('use-markup', True)
        delete_code = 4
        if (store_col != 3) and (store_col == (len(self.slot_config_store.variables)+2)):  # the user can only delete the last var col if it's not the only one
            d.add_buttons('Delete', delete_code)
        rename_code = 5
        d.add_buttons('Rename', rename_code)
        new_code = 6
        d.add_buttons('Add New', new_code)

        hb = Gtk.HBox()
        hb.add(Gtk.Label(label='New Variable Name: '))
        var_entry = Gtk.Entry()
        var_entry.set_text(col.get_title())
        hb.add(var_entry)
        mdb = d.get_message_area()
        mdb.add(hb)
        mdb.show_all()
        response = d.run()
        box_text = var_entry.get_text()
        d.destroy()
        if response != Gtk.ResponseType.CANCEL:
            if response == delete_code:
                self.slot_config_tv.remove_column(col)
                self.delete_variable()
            elif response == rename_code:
                col.set_title(box_text)
                self.slot_config_store.variables[store_col-3] = box_text
            elif response == new_code:
                self.add_variable(box_text)

    # deletes the last variable
    # can only delete the last one because deleting
    # any one besides that one can mess up all the existing col # refs
    # and fixing that seems hard
    # assumes the treeview col as already been deleted
    # updates the slot config store to remove the variable's data
    def delete_variable(self):
        variables = self.slot_config_store.variables
        del(variables[-1])
        # we must create the store in the proper shape
        create_params = tuple([str]*(len(list(self.slot_config_store[0]))-1))
        new_store = Gtk.ListStore(*create_params)  # ref des, user label, layout name, then the variable cols
        for row in self.slot_config_store:
            new_row = list(row)
            del(new_row[-1])
            new_store.append(tuple(new_row))
        self.slot_config_tv.set_model(new_store)
        del(self.slot_config_store)
        self.slot_config_store = new_store
        #self.slot_config_store.connect('row-changed', self.on_slot_store_change)
        self.slot_config_store.variables = variables

    # registers a new variable
    # adds the variable name to the variables list
    # inserts a col for it in the slot config treeview
    # and if add_store_col == True, appends a new col for it in the slot config liststore
    def add_variable(self, var_name, add_store_col=True):
        variables = self.slot_config_store.variables
        variables.append(var_name)
        store_col = 2 + len(variables)

        if add_store_col == True:
            # we must create the store in the proper shape
            create_params = tuple([str]*(len(list(self.slot_config_store[0]))+1))  
            new_store = Gtk.ListStore(*create_params)  # ref des, user label, layout name, then the variable cols
            for row in self.slot_config_store:
                new_row = list(row) + ['']
                new_store.append(tuple(new_row))
            self.slot_config_tv.set_model(new_store)
            del(self.slot_config_store)
            self.slot_config_store = new_store
            #self.slot_config_store.connect('row-changed', self.on_slot_store_change)
        self.slot_config_store.variables = variables

        var_cell = Gtk.CellRendererText()
        var_cell.set_property("editable", True)
        var_cell.connect("edited", self.on_slot_cell_edit, store_col)
        var_col = Gtk.TreeViewColumn(var_name, var_cell, text=store_col)
        var_col.set_clickable(True)
        var_col.connect('clicked', self.on_col_header_click, store_col)
        self.slot_config_tv.insert_column(var_col, store_col-1)
    
    def on_new_var_button(self, button):
        self.add_variable(self.b.get_object("new_var").get_text())

    # populates the slot config store with the default startup values
    def fill_slot_config_store(self, store, substrate_designators, labels, layouts):
        store.clear()
        for i in range(len(substrate_designators)):
            store.append([substrate_designators[i], labels[i], layouts[i]])

    # the user chose a new layout. save that choice in the slot config store
    def on_layout_combo_changed(self, widget, path, ti):
        store_col = 0
        self.slot_config_store[path][2] = widget.props.model[ti][store_col]
        GLib.idle_add(self.on_slot_store_change, self.slot_config_store, path, self.slot_config_store.get_iter(path), store_col)

    # the user has hovered their mouse over a layout choice
    def on_layout_combo_entered(self, widget, event, user_data):
        layout_index = user_data
        d = self.rotated_layout_drawings[self.layouts[layout_index]]
        svgh = Rsvg.Handle.new_from_data(d.asSvg().encode())
        self.layout_drawing_handle = svgh
        vb = svgh.get_intrinsic_dimensions()

        self.subs_pic.props.width_request = vb.out_width.length
        self.subs_pic.props.height_request = vb.out_height.length
        self.subs_pic.queue_draw()
        self.lopo.popup()
        self.lopo.show_all()

    # called when the substrate (lightmask) picture is being drawn
    def on_subs_pic_draw(self, drawing_area, cairo_context):
        if self.layout_drawing_handle is None:
            drawing_area.queue_draw()
        else:
            rh = self.layout_drawing_handle
            rh.render_cairo(cairo_context)

    # the layout ComboBox has now been magically created!
    # so let's install our hover-focus callbacks into its menu widget children
    def on_layout_combo_focus(self, treeview, combobox):
        try:
            popup_menu_widget = combobox.get_popup_accessible().props.widget
            popup_menu_widget.connect('unmap', lambda x: self.lopo.popdown())
            for i,c in enumerate(popup_menu_widget.get_children()):
                c.connect('enter-notify-event', self.on_layout_combo_entered, i)
        except:
            pass

    # the user has made a text edit in the slot config table
    def on_slot_cell_edit(self, widget, path, text, col):
        if col == 1:  # only sanitize col #1 (label)
            valid = re.compile('[^a-zA-Z0-9_\-\.]+', re.UNICODE)
            fill = valid.sub('', text)
        else:
            fill = text
        self.slot_config_store[path][col] = fill
        GLib.idle_add(self.on_slot_store_change, self.slot_config_store, path, self.slot_config_store.get_iter(path), col)

    # called when a user pushes a device selection toggle button
    # updates the store ticked and inconsistent values
    # for this row and all of its children and grandchildren
    # does not touch parents
    def on_dev_toggle(self, toggle, path, tree_col):
        store = tree_col.get_tree_view().get_model()
        checked = not toggle.get_active()
        store[path][1] = checked
        store[path][2] = False  # can't be inconsistent
        GLib.idle_add(self.calc_checkboxes, path, store, checked)
    
    def calc_checkboxes(self, path, store, checked):
        # make this selection flow down to all children and grandchildren
        titer = store.get_iter_from_string(str(path))  # this row's iterator
        citer = store.iter_children(titer)
        while citer is not None:
            store.set_value(citer, 1, checked)
            store.set_value(citer, 2, False)  # can't be inconsistent
            gciter = store.iter_children(citer)
            while gciter is not None:
                store.set_value(gciter, 1, checked)
                store.set_value(gciter, 2, False)  # can't be inconsistent
                gciter = store.iter_next(gciter)
            citer = store.iter_next(citer)
        GLib.idle_add(self.do_dev_store_update_tasks, store)
        #self.do_dev_store_update_tasks(store)

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
            path, col = tv.get_cursor()
            if tv.row_expanded(path) is True:
                tv.collapse_row(path)
            else:
                tv.expand_row(path)

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
            if self.mqtt_connecting == False:  # don't spam connections
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
        return True

    # a function that blocks non-alphanumeric text entry
    def only_alnum(self, widget, text, text_len, text_pos):
        if text_len > 0:
            allowed = '_-.'
            if text.isalnum():
                return True
            elif(text in allowed):
                return True
            else:
                widget.stop_emission_by_name('insert-text')

    # gets called on changes to text boxes that impact the run name
    def update_run_name(self, widget):
        un = self.b.get_object("user_name")
        unt = un.get_text()
        rnp = self.b.get_object("run_name_prefix")
        rnpt = rnp.get_text()
        rns = self.b.get_object("run_name_suffix")
        rnst = rns.get_text()

        if unt != "":
            run_name = f"{unt}{os.sep}"
        else:
            run_name=""
        run_name +=rnpt
        run_name +=rnst
        rn = self.b.get_object("run_name")
        rn.set_text(run_name)

    # draws the whole slot array
    def draw_array(self):
        max_render_pix = 600  # the picture's largest dim will be this many pixels
        dim = len(self.spacings)
        unit = self.spacings
        if dim == 1:
            unit = unit*2
        elif dim == 3:
            del(unit[-1])
        
        # size the canvas
        canvas = [0, 0]
        x_cens = [pos[0] for lab, pos in self.substrate_locations.items()]
        canvas[0] = max(x_cens) - min(x_cens) + unit[0]
        if dim == 1:
            canvas[1] = unit[1]
        else:
            y_cens = [pos[1] for lab, pos in self.substrate_locations.items()]
            canvas[1] = max(y_cens) - min(y_cens) + unit[1]

        d = draw.Drawing(canvas[0], canvas[1], origin='center', displayInline=False, **{'text-align':'center', 'font-family': 'monospace'})
        #d = draw.Drawing(max(canvas), max(canvas), origin='center', displayInline=False)
        #cmax = max(canvas)


        # try to see if we can hyperlink the background...
        # TODO: does not work atm, remove if this can't be fixed
        #class Hyperlink(draw.DrawingParentElement):
        #    TAG_NAME = 'a'
        #    def __init__(self, href, target=None, **kwargs):
        #        super().__init__(href=href, target=target, **kwargs)
        
        #hlink = Hyperlink('https://www.python.org', target='_blank')
        #hlink.append(draw.Rectangle(-canvas[0]/2, -canvas[1]/2, canvas[0], canvas[1], fill='white'))
        #d.append(hlink)
        #rh = self.array_drawing_handle
        #angle = self.config['UI']['gui_drawing_rotation_angle']
        #cairo_context.rotate(math.pi*angle/180)
        #rh.render_cairo(cairo_context)
        #print(rh.get_geometry_for_element(None)[-1].width)
        angle = self.config['UI']['gui_drawing_rotation_angle']
        rot = f"rotate({angle},0,0)"
        rg = draw.Group(**{"transform":rot})

        for row in self.slot_config_store:
            label = row[0]
            layout = row[2]
            pos = self.substrate_locations[label]
            lod = self.layout_drawings[layout]
            if dim == 1:
                t2 = f"translate({pos[0]},0)"
            else:
                t2 = f"translate({pos[0]},{pos[1]})"
            g = draw.Group(**{"transform":t2})
            for e in lod.allElements():
                g.append(e)
            big_font_size = max_render_pix/25
            lab = draw.Text(label, big_font_size, 0, -big_font_size/3, fill='lime', fill_opacity=0.9, **{'text-anchor':'middle','dominant-baseline':'central', 'font-weight':"bold"})  # , "transform":urot
            g.append(lab)
            #if not '1A' in label:
            #    d.append(g)
            rg.append(g)
            #d.append(g)
        d.append(rg)

        #maxd = max(canvas)
        #scale = max_render_pix/maxd
        #d.setPixelScale(scale)
        svg_handle = Rsvg.Handle.new_from_data(d.asSvg().encode())
        vb = svg_handle.get_intrinsic_dimensions().out_viewbox
        r = svg_handle.get_geometry_for_layer(None, vb).out_ink_rect
        vb_new = (r.x, r.y, r.width, r.height)
        d.viewBox = vb_new
        d.width = r.width
        d.height = r.height
        ndims = [r.width, r.height]
        scale = max_render_pix/max(ndims)
        d.setPixelScale(scale)
        svg_handle = Rsvg.Handle.new_from_data(d.asSvg().encode())

        self.array_drawing_handle = svg_handle

        self.array_pic.props.width_request = ndims[0]*scale
        self.array_pic.props.height_request = ndims[1]*scale
    
    def on_array_pic_draw(self, drawing_area, cairo_context):
        if self.array_drawing_handle is None:
            drawing_area.queue_draw()
        else:
            rh = self.array_drawing_handle
            #angle = self.config['UI']['gui_drawing_rotation_angle']
            #cairo_context.rotate(math.pi*angle/180)
            rh.render_cairo(cairo_context)

    # draws pixels based on layout info from the config file
    def draw_layout(self, pads, areas, locations, shapes, size, spacing, name):
        max_render_pix = 300  # how big in pixels should the canvas be on screen?
        d = len(spacing)
        canvas = spacing
        if d == 1:
            canvas = canvas*2
        elif d == 3:
            del(canvas[-1])
        maxd = max(canvas)
        scale = max_render_pix/maxd

        d  = draw.Drawing(canvas[0], canvas[1], origin='center', displayInline=False, **{'text-align':'center', 'font-family': 'monospace'})
        dr = draw.Drawing(canvas[0], canvas[1], origin='center', displayInline=False, **{'text-align':'center', 'font-family': 'monospace'})
        angle = self.config['UI']['gui_drawing_rotation_angle']

        n_pads = len(pads)

        wbg = draw.Rectangle(-canvas[0]/2, -canvas[1]/2, canvas[0], canvas[1], fill='white')
        d.append(wbg)

        if n_pads > 0:  # handles the empty case
            bg = draw.Rectangle(-size[0]/2, -size[1]/2, size[0], size[1], fill='black')
            d.append(bg)

        for i in range(n_pads):
            pad = pads[i]
            a = areas[i] * 100  # cm^2 to mm^2
            xy = locations[i]
            shape = shapes[i]
            if shape == 'c':
                r = math.sqrt(a/math.pi)
                d.append(draw.Circle(xy[0], xy[1], r, fill='white'))
            elif shape == 's':
                rx = math.sqrt(a)
                d.append(draw.Rectangle(xy[0]-rx/2, xy[1]-rx/2, rx, rx, fill='white'))
            elif isinstance(shape, float):
                rx = shape
                ry = a/rx
                d.append(draw.Rectangle(xy[0]-rx/2, xy[1]-ry/2, rx, ry, fill='white'))
            lab_font_size = scale/3
            lx = xy[0]
            ly = xy[1]-lab_font_size/3
            #urot = f"rotate({-angle},{lx},{ly})"
            lab = draw.Text(str(pad), lab_font_size, lx, ly, fill='gray', **{'text-anchor':'middle','dominant-baseline':'central', 'font-weight':"bold"})  # , "transform":urot
            d.append(lab)
        if ('OLD' in name) or ('legacy' in name):
            ll = maxd/4
            x1 = draw.Line(ll, ll, -ll, -ll, **{'stroke-width':f"{ll/8}","stroke":"red"})
            d.append(x1)
            x2 = draw.Line(ll, -ll, -ll, ll, **{'stroke-width':f"{ll/8}","stroke":"red"})
            d.append(x2)

        d.setPixelScale(scale)

        # now for the rotated version of this
        rot = f"rotate({angle},0,0)"
        g = draw.Group(**{"transform":rot})
        for e in d.allElements():
            g.append(e)
        dr.append(g)
        dr.setPixelScale(scale)

        return d, dr

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

    # fires every time a row is changed in the slot config store
    def on_slot_store_change(self, store, path, iter, col):
        system_label = store[iter][0]
        user_label = store[iter][1]
        layout = store[iter][2]

        try:
            pads  = self.config['substrates']['layouts'][layout]['pads']
            n_pix = len(pads)
            areas = self.config['substrates']['layouts'][layout]['areas']
        except:
            pads = []
            n_pix = float('nan')
            areas = None

        for store in [self.iv_store, self.eqe_store]:
            all_row = store.get_iter_first()
            slot_iter = store.iter_nth_child(all_row, int(str(path)))

            if col == 1:  # user label change
                # update the label in the device selection stores
                if user_label != "":
                    display_label = f"{system_label}: {user_label}"
                else:
                    display_label = system_label
                store.set_value(slot_iter, 0, display_label)
            elif col == 2:  # this was a layout change
                subs_check_visible = n_pix > 0
                store.set_value(slot_iter, 3, layout)
                store.set_value(slot_iter, 4, subs_check_visible)

                # is the parent checked?
                if store[slot_iter][1] == True:
                    checked = True
                else:
                    checked = False

                # re-fill the children with the updated info
                diter = store.iter_children(slot_iter)
                for pad, area in zip(pads, areas):
                    name_col_val = f"Device {pad}"  # what do we call this?
                    checked_col_val = checked  # is it selected?
                    expanded_col_val = False  # is the row expanded?
                    area_col_val = str(area)  # what's its area?
                    check_vis_col_val = True  # is the selection box visible?

                    if diter is None:  # we need to add a new row
                        diter = store.append(slot_iter, [name_col_val, checked_col_val, expanded_col_val, area_col_val, check_vis_col_val])
                    else:  # the row already exists let's just make sure it's right
                        store.set(diter, 0, name_col_val, 1, checked_col_val, 2, expanded_col_val, 3, area_col_val, 4, check_vis_col_val)
                    diter = store.iter_next(diter)

                # delete any extra children
                while (diter is not None) and store.iter_is_valid(diter):
                    store.remove(diter)

            GLib.idle_add(self.do_dev_store_update_tasks, store)
            #self.do_dev_store_update_tasks(store)

    # this function does the things that need to be done when something about a device store has changed
    # such as areas changed, pixels added to or removed from substrate, user label change or device selection change 
    # it
    # - ensures the device store ckeckbox booleans make sense.
    # by potentially modifying [2] incosistent, [4] visible  and [1] selected values for the top and substrate rows
    # - won't ever modify device row selections
    # - fills the bitmask text field
    # - writes the dev selection string
    # - computes the device dataframe containing relevant info on every device selected for measurement
    def do_dev_store_update_tasks(self, store):
        all_row = store.get_iter_first()  # the all row iterator
        siter = store.iter_children(all_row)  # the substrate iterator
        n_total = 0
        n_total_selected = 0
        bitmask = 0
        bitloc = 0
        
        df_cols=[]
        df_cols.append('system_label')  # label for the substrate slot that the system uses
        df_cols.append('user_label')  # label the user may have entered for this substrate
        df_cols.append('label')  # combo of the above two, formated as "{system}_{user}"
        df_cols.append('substrate_index')  # a number used to represent the substrate
        df_cols.append('layout_pixel_index')  # pixel index is for which pixel this is on its layout
        df_cols.append('pixel_offset_raw')  # offset of this pixel relative to the center of its substrate (as read from the config file)
        df_cols.append('pixel_offset')  # offset of this pixel relative to the center of its substrate (with unconfigured axes trimmed)
        df_cols.append('substrate_offset_raw')  # offset of this substrate in the array
        df_cols.append('substrate_offset')  # offset of this substrate in the array (with unconfigured axes trimmed)
        df_cols.append('loc_raw')  # offset of this pixel from center of substrate array
        df_cols.append('loc')  # offset of this pixel from center of substrate array  (with unconfigured axes trimmed)
        df_cols.append('layout')  # the layout name
        df_cols.append('area')  # illuminated area in cm^2
        df_cols.append('dark_area')  # active area in cm^2
        df_cols.append('mux_index')  # mux index is which mux switch needs to be closed for this (same as "pad" in cofig file)
        df_cols.append('mux_string')  # the string the firmware needs to select this pixel
        df_cols.append('user_vars')  # dictionary for the user variables
        df_cols += self.slot_config_store.variables  # experimental variable names
        df = pd.DataFrame(columns=df_cols)
        while siter is not None:  # substrate iterator loop
            n_subs = store.iter_n_children(siter)
            n_total += n_subs

            # set substrate level box visibility
            if n_subs == 0:
                # this substrate's picker is invisible
                store.set_value(siter, 4, False)
            else:
                store.set_value(siter, 4, True)
            diter = store.iter_children(siter)  # the device iterator
            n_subs_selected = 0
            while diter is not None:  # device iterator loop
                if store[diter][1] == True:  # device selected
                    bitmask += (1 << bitloc)

                    dfr = n_total_selected + n_subs_selected  # dataframe row
                    df.append(pd.Series(name=dfr, dtype=object))
                    dpath = str(store.get_path(diter))
                    dpath_split = dpath.split(':')
                    pixi = int(dpath_split[2])  # pixel index in tree
                    subi = int(dpath_split[1])  # substrate index in tree
                    system_label = self.slot_config_store[subi][0]
                    user_label = self.slot_config_store[subi][1]
                    layout = self.slot_config_store[subi][2]
                    df.at[dfr, 'layout_pixel_index'] = pixi
                    df.at[dfr, 'substrate_index'] = subi
                    df.at[dfr, 'system_label'] = system_label
                    df.at[dfr, 'user_label'] = user_label
                    if user_label == "":
                        df.at[dfr, 'label'] = system_label
                    else:
                        df.at[dfr, 'label'] = f"{system_label}_{user_label}"
                    df.at[dfr, 'layout'] = layout
                    df.at[dfr, 'area'] = self.config['substrates']['layouts'][layout]['areas'][pixi]
                    df.at[dfr, 'dark_area'] = self.config['substrates']['layouts'][layout]['dark_areas'][pixi]
                    mux_index = self.config['substrates']['layouts'][layout]['pads'][pixi]
                    df.at[dfr, 'mux_index'] = mux_index
                    df.at[dfr, 'mux_string'] = f"s{system_label}{mux_index}"
                    # the stage movements need to be reversed from the physical location offsets, so all
                    # these offset numbers get multiplied by -1 before getting stored in the output dataframe
                    por = self.config['substrates']['layouts'][layout]['locations'][pixi]
                    por = [p*-1 for p in por]
                    df.at[dfr, 'pixel_offset_raw'] = por
                    sor = self.substrate_locations[system_label]
                    sor = [s*-1 for s in sor]
                    df.at[dfr, 'substrate_offset_raw'] = sor
                    locr = []
                    for s,p in zip(sor,por):
                        locr.append(s+p)
                    df.at[dfr, 'loc_raw'] = locr
                    if self.num_axes < 3:
                        try:
                            del por[2]
                            del sor[2]
                            del locr[2]
                        except:
                            pass
                    if self.num_axes < 2:
                        try:
                            del por[1]
                            del sor[1]
                            del locr[1]
                        except:
                            pass
                    if self.num_axes < 1:
                        try:
                            del por[0]
                            del sor[0]
                            del locr[0]
                        except:
                            pass
                    df.at[dfr, 'pixel_offset'] = por
                    df.at[dfr, 'substrate_offset'] = sor
                    df.at[dfr, 'loc'] = locr
                    user_vars = {}
                    for i,var in enumerate(self.slot_config_store.variables):
                        df.at[dfr, var] = self.slot_config_store[subi][3+i]
                        user_vars[var] = self.slot_config_store[subi][3+i]
                    df.at[dfr, 'user_vars'] = user_vars

                    n_subs_selected += 1

                bitloc += 1
                diter = store.iter_next(diter)
            n_total_selected += n_subs_selected

            # set substrate level box state
            if n_subs_selected == 0:
                store.set_value(siter, 1, False)
                store.set_value(siter, 2, False)  # set consistent
            elif n_subs_selected == n_subs:
                store.set_value(siter, 1, True)
                store.set_value(siter, 2, False)  # set consistent
            else:
                store.set_value(siter, 1, False)
                store.set_value(siter, 2, True)  # set inconsistent

            siter = store.iter_next(siter)

        # set gui's hex text
        fmt_str = f"0{math.ceil(bitloc/4)}X"
        bitmask_text = f"0x{bitmask:{fmt_str}}"
        store_name = store.get_name()
        df.index.name = store_name.split()[0]
        if 'IV' in store_name:
            self.iv_dev_box.set_text(bitmask_text)
        elif 'EQE' in store_name:
            self.eqe_dev_box.set_text(bitmask_text)

        # attach the active pixel dataframe we just made to this store
        store.df = df

        # set top level box visiblility
        if n_total == 0:
            # top level picker is invisible
            store.set_value(all_row, 4, False)
        else:
            store.set_value(all_row, 4, True)

        # set top level checkbox state
        if n_total_selected == 0:
            store.set_value(all_row, 1, False)
            store.set_value(all_row, 2, False)  # set consistent
        elif n_total_selected == n_total:
            store.set_value(all_row, 1, True)
            store.set_value(all_row, 2, False)  # set consistent
        else:
            store.set_value(all_row, 1, False)
            store.set_value(all_row, 2, True)  # set inconsistent

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
        about_dialog.run()

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
        self.do_debug_tasks()

    def do_debug_tasks(self, *args, **kw_args):
        lg.debug("Hello World!")
        self.b.get_object("run_but").set_sensitive(True)
        #self.load_live_data_webviews(load=False)
        msg = {'cmd':'debug'}
        pic_msg = pickle.dumps(msg, protocol=pickle.HIGHEST_PROTOCOL)
        self.mqttc.publish("cmd/uitl", pic_msg, qos=2).wait_for_publish()
        print(self.slot_config_store.variables)
        self.draw_array()

    # fills the device selection bitmask text box based on the device selection treestore
    def open_dev_picker(self, button):
        button_name = button.get_name()
        if 'IV' in button_name:
            po_target_id = 'iv_devs'
            store = self.iv_store
        elif 'EQE' in button_name:
            po_target_id = 'eqe_devs'
            store = self.eqe_store
        try:
            self.device_select_tv.set_model(store)
            self.po.set_relative_to(self.b.get_object(po_target_id))
            self.device_select_tv.expand_row(Gtk.TreePath("0"), False)  # top is always expanded
            self.po.show_all()
        except:
            pass

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
                    gui_data[id_str] = {"type": str(type(this_obj)), "value": this_obj.get_text(), "call_to_set": "set_buffer_text"}

        # handle the treestores and liststores manually because they can't be pickled, plus they're not registered in glade
        for store in ['iv_store', 'eqe_store', 'slot_config_store']:
            this_obj = getattr(self, store)
            store_data = []
            this_obj.foreach(lambda model, path, it: store_data.append([str(path),tuple(model[it])]))
            gui_data[store] = {"type": str(type(this_obj)), "value": store_data, "call_to_set": "fill_store"}
            if hasattr(this_obj, 'variables'):
                gui_data['variables'] = {"type": "variables", "value": this_obj.variables, "call_to_set": "set_variables"}

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

            # ids that we don't want to load into the gui
            dont_load = ['run_name_suffix', 'run_name']

            # pop away the things we don't want
            [load_data.pop(key) for key in dont_load]

            stores = {}  # a place to hold the stores we read. they need to be filled last
            for id_str, obj_info in load_data.items():
                this_type = obj_info['type']
                if ('TreeStore' in this_type) or ('ListStore' in this_type): # for handling liststores and treestores
                    stores[id_str] = obj_info  # save the stores for later handling when we see them
                elif this_type == 'variables':  # deal with the user's custom variables here
                    vars = obj_info['value']
                    create_params = tuple([str]*(3+len(vars)))  # we must create the store in the proper shape
                    self.slot_config_store = Gtk.ListStore(*create_params)  # ref des, user label, layout name, then the variable cols
                    #self.slot_config_store.connect('row-changed', self.on_slot_store_change)
                    self.slot_config_store.variables = []
                    # now that the store is the right shape, we can remake the treeview cols
                    self.setup_slot_config_tv(self.slot_config_tv, self.layouts)
                    self.slot_config_tv.set_model(self.slot_config_store)
                    for var in vars:
                        self.add_variable(var, add_store_col=False)
                else:  # all the other gui params can be handled simply
                    try:
                        this_obj = self.b.get_object(id_str)
                        if obj_info['call_to_set'] == 'set_buffer_text':
                            b = this_obj.get_buffer()
                            b.set_text(obj_info['value'], -1)
                        else:
                            call_to_set = getattr(this_obj, obj_info['call_to_set'])
                            call_to_set(obj_info['value'])
                    except:
                        lg.info(f"Load issue with: {id_str}")
                        #pass  # give up if we can't load this one element

            # now we can update the stores since we know we have shape for the slot config store set properly
            # but the slot config one has to go first
            id = "slot_config_store"
            self.load_store(id, stores[id])
            del stores[id]

            # now we can load the rest of the stores
            for id_str, obj_info in stores.items():
                self.load_store(id_str, obj_info)
            self.update_gui()
        else:
            lg.info(f"Load aborted.")

    # load a store from the save file format into the gui's GTK format
    def load_store(self, id, info):
        this_type = info['type']
        list_store = 'ListStore' in this_type
        try:
            store = getattr(self, id)
            store.clear()
            for line in info['value']:
                path = line[0]
                row = line[1]
                if list_store:
                    store.append(row)
                else:  # treestore is a bit more complicated because the tree structure must be recreated
                    if ':' not in path:
                        piter = None  # no parent iterator, top level
                    else:
                        parent_path = path.rsplit(':',1)[0]
                        piter = store.get_iter_from_string(parent_path)
                    store.append(piter, row)
            if ('iv_store' == id) or ('eqe_store' == id):  # for the device stores
                # make sure the data frames are correct
                self.do_dev_store_update_tasks(store)
        except:
            lg.debug(f"Failed to load store: {id}")
            pass  # give up on this one store if we can't load it

    # looks at where the popover is
    # to determint which device selection store we want
    def get_store(self, target_id=""):
        # where is the popover right now?
        if target_id == "":
            target_id = Gtk.Buildable.get_name(self.po.get_relative_to())
        if "eqe" in target_id:
            store = self.eqe_store
        elif "iv" in target_id:
            store = self.iv_store
        else:
            store = None
        return store

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
            'pads': some_lists['sub_dev_nums'],
            'pcb': self.config['controller']['address'],
            'smu': self.config['smu']
            }
        pic_msg = pickle.dumps(msg, protocol=pickle.HIGHEST_PROTOCOL)
        self.mqttc.publish("cmd/uitl", pic_msg, qos=2).wait_for_publish()

    # this is for mux device toggle button in the utility view
    def on_device_toggle(self, button):
        """
        allows the user to connect any one pixel, or disconnect them all
        toggels between connecting the first selected IV device and selecting nothing
        """
        if self.all_mux_switches_open == True:
            self.all_mux_switches_open = False
            active = self.iv_store.df
            if len(active) == 0:
                lg.info("No devices selected for connection")
            else:
                first_dev = active.iloc[0]
                lg.info(f"Connecting device: {first_dev['label']}-{first_dev['mux_index']}")
                msg = {}
                msg['cmd'] = 'for_pcb'
                msg['pcb'] = self.config['controller']['address']
                msg['pcb_virt'] = self.config['controller']['virtual']
                msg['pcb_cmd'] = first_dev['mux_string']
                pic_msg = pickle.dumps(msg, protocol=pickle.HIGHEST_PROTOCOL)
                self.mqttc.publish("cmd/uitl", pic_msg, qos=2).wait_for_publish()
        else:
            self.all_mux_switches_open = True
            lg.info("Disconnecting all devices")
            msg = {}
            msg['cmd'] = 'for_pcb'
            msg['pcb'] = self.config['controller']['address']
            msg['pcb_virt'] = self.config['controller']['virtual']
            msg['pcb_cmd'] = "s"
            pic_msg = pickle.dumps(msg, protocol=pickle.HIGHEST_PROTOCOL)
            self.mqttc.publish("cmd/uitl", pic_msg, qos=2).wait_for_publish()

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
        msg = {}
        msg['cmd'] = 'for_pcb'
        msg['pcb'] = self.config['controller']['address']
        msg['pcb_virt'] = self.config['controller']['virtual']
        msg['pcb_cmd'] = pcb_cmd
        pic_msg = pickle.dumps(msg, protocol=pickle.HIGHEST_PROTOCOL)
        self.mqttc.publish("cmd/uitl", pic_msg, qos=2).wait_for_publish()

    def on_health_button(self, button):
        lg.info("HEALTH CHECK INITIATED")
        msg = {'cmd':'check_health'}
        msg['pcb'] = self.config['controller']['address']
        if self.enable_psu == True:
            msg['psu'] = self.config['psu']['address']
        if self.enable_smu == True:
            msg['smu_address'] =self.config['smu']['address']
            msg['smu_baud'] = self.config['smu']['baud']
            msg['smu_le'] = self.config['smu']['terminator']
        if self.enable_lia == True:
            msg['lia_address'] = self.config['lia']['address']
        if self.enable_mono == True:
            msg['mono_address'] = self.config['monochromator']['address']
        if self.enable_solarsim == True:
            msg['le_address'] = self.config['solarsim']['address']
            msg['le_recipe'] = self.b.get_object("light_recipe").get_text()
        pic_msg = pickle.dumps(msg, protocol=pickle.HIGHEST_PROTOCOL)
        self.mqttc.publish("cmd/uitl", pic_msg, qos=2).wait_for_publish()

    def move_warning(self):
        if self.enable_stage == True:
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
        else:
            result = Gtk.ResponseType.OK
        return(result)

    def on_home_button(self, button):
        """Home the stage."""
        if (self.move_warning() == Gtk.ResponseType.OK):
            lg.info("Requesting stage home...")
            msg = {}
            msg['cmd'] = 'home'
            msg['pcb'] = self.config['controller']['address']
            msg['pcb_virt'] = self.config['controller']['virtual']
            msg['stage_uri'] = self.config['stage']['uri']
            msg['stage_virt'] = self.config['stage']['virtual']
            pic_msg = pickle.dumps(msg, protocol=pickle.HIGHEST_PROTOCOL)
            self.mqttc.publish("cmd/util", pic_msg, qos=2).wait_for_publish()

    def on_halt_button(self, button):
        """Emergency stop"""
        lg.warning("Powering down the stage motor drivers")
        msg = {}
        msg['cmd'] = 'estop'
        msg['pcb'] = self.config['controller']['address']
        msg['pcb_virt'] = self.config['controller']['virtual']
        pic_msg = pickle.dumps(msg, protocol=pickle.HIGHEST_PROTOCOL)
        self.mqttc.publish("cmd/uitl", pic_msg, qos=2).wait_for_publish()
        self.mqttc.publish("measurement/stop", "stop", qos=2).wait_for_publish()

    def on_mono_zero_button(self, button):
        """Sends Monochromator to 0nm"""
        msg = {}
        msg['cmd'] = 'mono_zero'
        msg['mono_address'] = self.config['monochromator']['address']
        msg['mono_virt'] = self.config['monochromator']['virtual']
        pic_msg = pickle.dumps(msg, protocol=pickle.HIGHEST_PROTOCOL)
        self.mqttc.publish("cmd/util", pic_msg, qos=2).wait_for_publish()

    def on_stage_read_button(self, button):
        """Read the current stage position."""
        msg = {}
        msg['cmd'] = 'read_stage'
        msg['pcb'] = self.config['controller']['address']
        msg['pcb_virt'] = self.config['controller']['virtual']
        msg['stage_uri'] = self.config['stage']['uri']
        msg['stage_virt'] = self.config['stage']['virtual']
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
            msg = {}
            msg['cmd'] = 'goto'
            msg['pcb'] = self.config['controller']['address']
            msg['pcb_virt'] = self.config['controller']['virtual']
            msg['stage_uri'] = self.config['stage']['uri']
            msg['stage_virt'] = self.config['stage']['virtual']
            msg['pos'] = pos
            pic_msg = pickle.dumps(msg)
            self.mqttc.publish("cmd/util", pic_msg, qos=2).wait_for_publish()

    def on_run_button(self, button):
        """Send run info to experiment orchestrator via MQTT."""
        if (self.move_warning() == Gtk.ResponseType.OK):
            run_name = self.b.get_object("run_name").get_text()
            gui_data = self.harvest_gui_data()
            run_name = pathlib.Path(run_name)  #run name can now be a path

            try:
                autosave_config = self.config['meta']['autosave_enabled'] == True
            except:
                autosave_config = True
            
            if autosave_config == True:
                autosave_file_name = run_name.name + '_autosave.dat'
                try:
                    user_autosave_path = self.config['meta']['autosave_path']
                except:
                    user_autosave_path = 'runconfigs'
                if pathlib.Path(user_autosave_path).is_absolute():
                    autosave_root = pathlib.Path(user_autosave_path)
                else:
                    autosave_root = pathlib.Path.home() / user_autosave_path
                autosave_pathname = autosave_root / run_name.parent

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
        
        # send almost every gui item up
        for key, val in gui_dict.items():
            # don't send up the stores, those are only for the gui
            # and their data is in the dataframes handled below
            if not key.endswith('store'):
                args[key] = val['value']
        
        # do some scaling and selection logic for the backend
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

        # so that the data consumer knows what to look out for
        args['pixel_data_object_names'] = []

        # the potential pixel data objects we'll send (empty ones are not sent)
        dfs = [self.eqe_store.df, self.iv_store.df]

        # whitelist some pixel data frame cols for the saver.
        # only these cols should get saved to file
        saver_whitelist_cols = []
        saver_whitelist_cols.append('system_label')
        saver_whitelist_cols.append('user_label')
        saver_whitelist_cols.append('substrate_index')
        saver_whitelist_cols.append('layout')
        saver_whitelist_cols.append('area')
        saver_whitelist_cols.append('dark_area')
        saver_whitelist_cols.append('mux_index')
        saver_whitelist_cols += self.slot_config_store.variables
        args['pix_cols_to_save'] = saver_whitelist_cols

        # include the enable/disables
        args['enable_stage'] = self.enable_stage
        args['enable_eqe'] = self.enable_eqe
        args['enable_iv'] = self.enable_iv
        args['enable_lia'] = self.enable_lia
        args['enable_mono'] = self.enable_mono
        args['enable_psu'] = self.enable_psu
        args['enable_solarsim'] = self.enable_solarsim
        args['enable_smu'] = self.enable_smu

        # package up the data for each pixel
        for df in dfs:
            # only send ones with selected pixels
            if len(df) > 0:                
                key = f"{df.index.name}_stuff"
                args[key] = df
                args['pixel_data_object_names'].append(key)

                # for debugging
                #print(df.to_markdown())

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
        
        if active_title == 'Array Overview':
            self.draw_array()
        
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

# adapted from https://gist.github.com/mahmoud/db02d16ac89fa401b968#gistcomment-2884354
def merge_dicts(target_list, replace_lists=True):

    target_list = [(id(t), t) for t in target_list]

    ret = None

    def remerge_enter(path, key, value):
        new_parent, new_items = default_enter(path, key, value)
        if ret and not path and key is None:
            new_parent = ret
        try:
            cur_val = get_path(ret, path + (key, ))
        except KeyError:
            pass
        else:
            new_parent = cur_val

        if isinstance(value, list):
            if replace_lists:
                new_parent = value
            else:
                new_parent.extend(value)  # concatenate lists
            new_items = []

        return new_parent, new_items

    for t_name, target in target_list:
        remerge_visit = default_visit
        ret = remap(target, enter=remerge_enter, visit=remerge_visit)

    return ret


if __name__ == "__main__":
    app = App()
    app.run(sys.argv)
