# Licensed under a 3-clause BSD style license - see LICENSE.rst

"""
This class supports communication with a Ginga-based viewer.

For default key and mouse shortcuts in a Ginga window, see:
    https://ginga.readthedocs.org/en/latest/quickref.html

"""

from __future__ import print_function, division, absolute_import

import sys
import os
import traceback
import time
import warnings
import logging
import threading
import numpy as np

from . import util
from astropy.io import fits

from ginga.misc import log, Settings
from ginga.AstroImage import AstroImage
from ginga import cmap
from ginga.util import paths

from matplotlib import get_backend
import matplotlib.pyplot as plt

# module variables
_matplotlib_cmaps_added = False

#the html5 viewer is currently supported
__all__ = ['ginga','ginga_general']

class ginga_general(object):

    """ A base class which controls all interactions between the user and the
    ginga window

        The ginga_??() contructor creates a new window with either the
        matplotlib backend or through a jupyter notebook

        Parameters
        ----------
        close_on_del : boolean, optional
            If True, try to close the window when this instance is deleted.


        Attributes
        ----------
        view: Ginga view object
             The object instantiated from a Ginga view class

        exam: imexamine object
    """

    def __init__(self, exam=None, close_on_del=True, logger=None, port=None):
        """
        Notes
        -----
        Ginga viewers all need a logger, if none is provided it will create one

        the port option is for use in the Jupyter notebook since the server
        displays the image to a distict port. The user can choose to have multiple
        windows open at the same time as long as they have different ports.

        """
        global _matplotlib_cmaps_added
        self.port = port
        self.exam = exam
        self._close_on_del = close_on_del
        # dictionary where each key is a frame number, and the values are a
        # dictionary of details about the image loaded in that frame
        self._viewer=dict()
        self._current_frame = 1
        self._current_slice = None

        # ginga view object, created in subclass
        self.ginga_view = None

        # set up possible color maps
        self._define_cmaps()

        # for synchronizing on keystrokes
        self._cv = threading.RLock()
        self._kv = []
        self._capturing = False

        # ginga objects need a logger, create a null one if we are not
        # handed one in the constructor
        if logger is None:
            logger = log.get_logger(null=True)
        self.logger = logger
        self._saved_logger = logger
        self._debug_logger = log.get_logger(level=10, log_stderr=True)

        # Establish settings (preferences) for ginga viewers
        basedir = paths.ginga_home
        self.prefs = Settings.Preferences(
            basefolder=basedir,
            logger=self.logger)

        # general preferences shared with other ginga viewers
        self.settings = self.prefs.createCategory('general')
        self.settings.load(onError='silent')
        self.settings.setDefaults(useMatplotlibColormaps=False,
                             autocuts='on', autocut_method='zscale')

        # add matplotlib colormaps to ginga's own set if user has this
        # preference set
        if self.settings.get('useMatplotlibColormaps', False) and \
                (not _matplotlib_cmaps_added):
            # Add matplotlib color maps if matplotlib is installed
            try:
                cmap.add_matplotlib_cmaps()
                _matplotlib_cmaps_added = True
            except Exception as e:
                print(
                    "Failed to load matplotlib colormaps: {0}".format(
                        str(e)))

        # bindings preferences shared with other ginga viewers
        bind_prefs = self.prefs.createCategory('bindings')
        bind_prefs.load(onError='silent')

        # viewer preferences unique to imexam ginga viewers
        viewer_prefs = self.prefs.createCategory('imexam')
        viewer_prefs.load(onError='silent')

        # create the viewer specific to this backend
        self._create_viewer(bind_prefs, viewer_prefs)

        # enable all interactive ginga features
        bindings = self.ginga_view.get_bindings()
        bindings.enable_all(True)
        self.ginga_view.add_callback('key-press', self._key_press_normal)
        self.canvas.enable_draw(False)
        self.canvas.add_callback('key-press', self._key_press_imexam)
        self.canvas.setSurface(self.ginga_view)
        self.canvas.ui_setActive(True)

    def _draw_imexam_indicator(self):
        return
        # -- Here be black magic ------
        # This function draws the imexam indicator on the lower left
        # hand corner of the canvas

        try:
            # delete previous indicator, if there was one
            self.canvas.deleteObjectByTag('indicator')
        except:
            pass

        # assemble drawing classes

        Text = self.canvas.getDrawClass('text')
        Rect = self.canvas.getDrawClass('rectangle')
        Compound = self.canvas.getDrawClass('compoundobject')

        # calculations for canvas coordinates
        mode = 'imexam'
        xsp, ysp = 6, 6
        wd, ht = self.ginga_view.get_window_size()
        #x1, y1 = wd-12*len(mode), ht-12
        x1, y1 = 12, 12
        o1 = Text(x1, y1, mode,
                  fontsize=12, color='orange', coord='canvas')
        #o1.fitsimage = self.view
        wd, ht = o1.get_dimensions()

        # yellow text on a black filled rectangle
        o2 = Compound(Rect(x1 - xsp, y1 - ht - ysp, x1 + wd + xsp, y1 + ht + ysp,
                           color='black',
                           fill=True, fillcolor='black', coord='canvas'),
                      o1, coord='canvas')

        # use canvas, not data coordinates

        self.canvas.add(o2, tag='indicator')
        # -- end black magic ------

    def _create_viewer(self, bind_prefs, viewer_prefs):
        """Create backend-specific viewer."""
        raise Exception("Subclass should override this method!")

    def _capture(self):
        """
        Insert our canvas so that we intercept all events before they reach
        processing by the bindings layer of Ginga.
        """
        self.ginga_view.onscreen_message("Entering imexam mode",
                                         delay=0.5)
        # insert the canvas
        self.ginga_view.add(self.canvas, tag='imexam-canvas')
        self._capturing = True
        self._draw_imexam_indicator()

    def _release(self):
        """
        Remove our canvas so that we no longer intercept events.
        """
        self.ginga_view.onscreen_message("Leaving imexam mode",
                                         delay=1.0)
        self._capturing = False
        self.canvas.deleteObjectByTag('indicator')

        # retract the canvas
        self.ginga_view.deleteObjectByTag('imexam-canvas')

    def __str__(self):
        return "<ginga viewer>"

    def __del__(self):
        if self._close_on_del:
            self.close()

    def _set_frameinfo(self, fname=None, hdu=None, data=None,
                       image=None):
        """Set the name and extension information for the data displayed in
        the frame and gather header information.

        Notes
        -----
        """
        # check the current frame, if none exists, then don't continue
        frame=self.frame()
        if frame:
            if frame not in self._viewer.keys():
                self._viewer[self._current_frame] = dict()

            if data is None or not data.any():
                try:
                    data = self._viewer[self._current_frame]['user_array']
                except KeyError:
                    pass

            extver = None  # extension number
            extname = None  # name of extension
            filename = None  # filename of image
            numaxis = 2  # number of image planes, this is NAXIS
            # tuple of each image plane, defaulted to 1 image plane
            naxis = (0)
            # data has more than 2 dimensions and loads in cube/slice frame
            iscube = False
            mef_file = False  # used to check misleading headers in fits files

            if hdu:
                pass

            # update the viewer dictionary, if the user changes what's displayed in a frame this should update correctly
            # this dictionary will be referenced in the other parts of the code. This enables tracking user arrays through
            # frame changes

            self._viewer[self._current_frame] = {'filename': fname,
                                   'extver': extver,
                                   'extname': extname,
                                   'naxis': naxis,
                                   'numaxis': numaxis,
                                   'iscube': iscube,
                                   'user_array': data,
                                   'image': image,
                                   'hdu': hdu,
                                   'mef': mef_file}

    def valid_data_in_viewer(self):
        """return bool if valid file or array is loaded into the viewer"""
        frame = self._current_frame

        if self._viewer[frame]['filename']:
            return True
        else:
            try:
                if self._viewer[frame]['user_array'].any():
                    valid = True
                elif self._viewer[frame]['hdu'].any():
                    valid = True
                elif self._viewer[frame]['image'].any():
                    valid = True
            except AttributeError as ValueError:
                valid = False
                print("error in array")

            return valid

    def get_filename(self):
        """return the filename currently on display"""
        frame = self.frame()
        if frame:
            return self._viewer[frame]['filename']

    def get_frame_info(self):
        """return more explicit information about the data displayed in the current frame"""
        return self._viewer[self.frame()]

    def get_viewer_info(self):
        """Return a dictionary of information about all frames which are loaded with data"""
        return self._viewer

    def close(self):
        """ close the window"""
        plt.close(self.figure)

    def readcursor(self):
        """
        returns image coordinate postion and key pressed,
        """
        # insert canvas to trap keyboard events if not already inserted
        if not self._capturing:
            self._capture()

        with self._cv:
            self._kv = ()

        # wait for a key press
        # NOTE: the viewer now calls the functions directly from the
        # dispatch table, and only returns on the quit key here
        while True:
            # ugly hack to suppress deprecation  by mpl
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                # run event loop, so window can get a keystroke
                #but only depending on context
                if self.use_opencv():
                    self.figure.canvas.start_event_loop(timeout=0.1)

            with self._cv:
                # did we get a key event?
                if len(self._kv) > 0:
                    (k, x, y) = self._kv
                    break

        # ginga is returning 0 based indexes
        return x + 1, y + 1, k

    def _define_cmaps(self):
        """setup the default color maps which are available"""

        # get ginga color maps
        self._cmap_colors = cmap.get_names()

    def cmap(self, color=None, load=None, invert=False, save=False,
             filename='colormap.ds9'):
        """ Set the color map table to something else, using a defined list of options


        Parameters
        ----------
        color: string
            color must be set to one of the available DS9 color map names

        load: string, optional
            set to the filename which is a valid colormap lookup table
            valid contrast values are from 0 to 10, and valid bias values are from 0 to 1

        invert: bool, optional
            invert the colormap

        save: bool, optional
            save the current colormap as a file

        filename: string, optional
            the name of the file to save the colormap to

        """

        if color:
            if color in self._cmap_colors:
                self.ginga_view.set_color_map(color)
            else:
                print("Unrecognized color map, choose one of these:")
                print(self._cmap_colors)

        # these should be pretty easy to support if we use matplotlib
        # to load them
        if invert:
            warnings.warn("Colormap invert not supported")

        if load:
            warnings.warn("Colormap loading not supported")

        if save:
            warnings.warn("Colormap saving not supported")

    def frame(self):
        """convenience function to report frames
        currently only 1 frame is supported per object in HTML5
        """
        return self._current_frame

    def iscube(self):
        """return information on whether a cube image is displayed in the current frame"""
        frame = self._current_frame
        if frame:
            return self._viewer[frame]['iscube']

    def get_slice_info(self):
        """return the slice tuple that is currently displayed"""
        frame = self._current_frame

        if self._viewer[frame]['iscube']:
            image_slice = self._viewer[frame]['naxis']
        else:
            image_slice = None
        return image_slice

    def get_data(self):
        """ return a numpy array of the data displayed in the current frame
        """

        frame = self._current_frame
        if frame:
            if isinstance(self._viewer[frame]['user_array'], np.ndarray):
                return self._viewer[frame]['user_array']

            elif self._viewer[frame]['hdu'] != None:
                return self._viewer[frame]['hdu'].data

            elif self._viewer[frame]['image'] != None:
                return self._viewer[frame]['image'].get_data()

    def get_header(self):
        """return the current fits header as a string or None if there's a problem"""

        # TODO return the simple header for arrays which are loaded

        if frame and self._viewer[self._current_frame]['hdu'] != None:
            hdu = self._viewer[frame]['hdu']
            return hdu.header
        else:
            warnings.warn("No file with header loaded into ginga")
            return None

    def _key_press_normal(self, canvas, keyname):
        """
        This callback function is called when a key is pressed in the
        ginga window without the canvas overlaid.  It's sole purpose is to
        recognize an 'i' to put us into 'imexam' mode.
        """
        if keyname == 'i':
            self._capture()
            return True
        return False

    def _key_press_imexam(self, canvas, keyname):
        """
        This callback function is called when a key is pressed in the
        ginga window with the canvas overlaid.  It handles all the
        dispatch of the 'imexam' mode.
        """
        data_x, data_y = sself.ginga_view.get_last_data_xy()
        self.logger.debug("key %s pressed at data %f,%f" % (
            keyname, data_x, data_y))

        if keyname == 'i':
            # temporarily switch to non-imexam mode
            self._release()
            return True

        elif keyname == 'backslash':
            # exchange normal logger for the stdout debug logger
            if self.logger != self._debug_logger:
                self.logger = self._debug_logger
                self.ginga_view.onscreen_message("Debug logging on",
                                                 delay=1.0)
            else:
                self.logger = self._saved_logger
                self.ginga_view.onscreen_message("Debug logging off",
                                                 delay=1.0)
            return True

        elif keyname == 'q':
            # exit imexam mode
            self._release()

            with self._cv:
                # this will be picked up by the caller in readcursor()
                self._kv = (keyname, data_x, data_y)
            return True

        # get our data array
        data = self.get_data()
        self.logger.debug(
            "x,y,data dim: %f %f %i" %
            (data_x, data_y, data.ndim))
        self.logger.debug("exam=%s" % str(self.exam))
        # call the imexam function directly
        if self.exam is not None:
            try:
                method = self.exam.imexam_option_funcs[keyname][0]
            except KeyError:
                self.logger.debug(
                    "no method defined in the option_funcs dictionary")
                return False

            self.logger.debug(
                "calling examine function key={0}".format(keyname))
            try:
                method(data_x, data_y, data)
            except Exception as e:
                self.logger.error("Failed examine function: %s" % (str(e)))
                try:
                    # log traceback, if possible
                    (type, value, tb) = sys.exc_info()
                    tb_str = "".join(traceback.format_tb(tb))
                    self.logger.error("Traceback:\n%s" % (tb_str))
                except Exception:
                    tb_str = "Traceback information unavailable."
                    self.logger.error(tb_str)

        return True

    def load_fits(self, fname="", extver=1, extname=None):
        """convenience function to load fits image to current frame

        Parameters
        ----------
        fname: string, optional
            The name of the file to be loaded. You can specify the full extension in the name, such as
            filename_flt.fits[sci,1] or filename_flt.fits[1]

        extver: int, optional
            The extension to load (EXTVER in the header)

        extname: string, optional
            The name (EXTNAME in the header) of the image to load

        Notes
        -----
        """
        if fname:
            # see if the image is MEF or Simple
            fname = os.path.abspath(fname)
            short = True
            try:
                mef = util.check_filetype(fname)
                if not mef:
                    extver = 0
                cstring = util.verify_filename(fname, getshort=short)
                image = AstroImage(logger=self.logger)

                with fits.open(cstring) as filedata:
                    hdu = filedata[extver]
                    image.load_hdu(hdu)

            except Exception as e:
                self.logger.error("Exception opening file: {0}".format(e))
                raise IOError(str(e))

            self._set_frameinfo(fname=fname, hdu=hdu, image=image)
            self.ginga_view.set_image(image)

        else:
            print("No filename provided")

    def panto_image(self, x, y):
        """convenience function to change to x,y  physical image coordinates


        Parameters
        ----------
        x: float
            X location in physical coords to pan to

        y: float
            Y location in physical coords to pan to


        """
        # ginga deals in 0-based coords
        x, y = x - 1, y - 1

        self.ginga_view.set_pan(x, y)

    def panto_wcs(self, x, y, system='fk5'):
        """pan to wcs location coordinates in image


        Parameters
        ----------

        x: string
            The x location to move to, specified using the given system
        y: string
            The y location to move to
        system: string
            The reference system that x and y were specified in, they should be understood by DS9

        """
        # this should be replaced by querying our own copy of the wcs
        image = self.ginga_view.get_image()
        a, b = image.radectopix(x, y, coords='data')
        self.ginga_view.set_pan(a, b)

    def rotate(self, value=None):
        """rotate the current frame (in degrees), the current rotation is printed with no params

        Parameters
        ----------

        value: float [degrees]
            Rotate the current frame {value} degrees
            If value is None, then the current rotation is printed

        """
        if value is not None:
            self.ginga_view.rotate(value)

        rot_deg = self.ginga_view.get_rotation()
        print("Image rotated at {0:f} deg".format(rot_deg))

    def transform(self, flipx=None, flipy=None, flipxy=None):
        """transform the frame

        Parameters
        ----------

        flipx: boolean
            if True flip the X axis, if False don't, if None leave current
        flipy: boolean
            if True flip the Y axis, if False don't, if None leave current
        swapxy: boolean
            if True swap the X and Y axes, if False don't, if None leave current
        """
        _flipx, _flipy, _swapxy = self.ginga_view.get_transform()

        # preserve current transform if not supplied as a parameter
        if flipx is None:
            flipx = _flipx
        if flipy is None:
            flipy = _flipy
        if swapxy is None:
            swapxy = _swapxy

        self.ginga_view.transform(flipx, flipy, swapxy)

    def snapsave(self):
        """save a frame display as a PNG file

        Parameters
        ----------

        filename: string
            The name of the output PNG image

        """
        self.ginga_view.show()

    def scale(self, scale='zscale'):
        """ The default zscale is the most widely used option

        Parameters
        ----------

        scale: string
            The scale for ds9 to use, these are set strings of
            [linear|log|pow|sqrt|squared|asinh|sinh|histequ]

        """

        # setting the autocut method?
        mode_scale = self.ginga_view.get_autocut_methods()

        if scale in mode_scale:
            self.ginga_view.set_autocut_params(scale)
            return

        # setting the color distribution algorithm?
        color_dist = self.ginga_view.get_color_algorithms()

        if scale in color_dist:
            self.ginga_view.set_color_algorithm(scale)
            return

    def view(self, img):
        """ Display numpy image array to current frame

        Parameters
        ----------
        img: numpy array
            The array containing data, it will be forced to numpy.array()

        Examples
        --------
        view(np.random.rand(100,100))

        """

        frame = self.frame()
        if not frame:
            print("No valid frame")
        else:
            img_np = np.array(img)
            image = AstroImage(img_np, logger=self.logger)
            self._set_frameinfo(data=img_np, image=image)
            self.ginga_view.set_image(image)

    def zoomtofit(self):
        """convenience function for zoom"""
        self.ginga_view.zoom_fit()

    def zoom(self, zoomlevel):
        """ zoom using the specified level

        Parameters
        ----------
        zoomlevel: integer

        Examples
        --------
        zoom(6)
        zoom(-3)

        """

        try:
            self.ginga_view.zoom_to(zoomlevel)

        except Exception as e:
            print("problem with zoom: %s" % str(e))

    def grab(self):
        raise NotImplementedError
        
class ginga(ginga_general):

    """
    A ginga-based viewer that uses an HTML5 window in the browser.
    This is compatible with the jupyter notebook and can be run from a server.

    This kind of viewer has slower performance than if we
    choose some widget back ends, but the advantage is that
    it works so long as the user has a working browser.

    This example illustrates using a Ginga widget in a web browser,  All the
    rendering is done on the server side and the browser only acts as a display
    front end.  Using this you could create an analysis type environment on a
    server and view it via a browser or from a jupyter notebook.
    """

    def _open_browser(self):
        try:
            import webbrowser
            webbrowser.open_new_tab(self.ginga_view.url)
        except ImportError:
            warnings.warn(
                "webbrowser module not installed, see the installed doc directory for the HTML help pages")
            print("Open a new browser window for: {}".format(self.ginga_view.url()))


    def _get_open_port(self):
        """
        Try and assign an open port automatically
        """
        import socket
        for port in range(9904,9999):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            remote  = socket.gethostbyname("localhost")
            result = s.connect_ex((remote, port))
            if result == 0:
                self.port = port #should be unused
                print("connecting on port {%i}:".format(port))
                s.close()
                break

    def _create_viewer(self, bind_prefs, viewer_prefs, opencv=False, threads=1):
        """Ginga imports for  display in an HTML5 browser"""
        from ginga.web.pgw import ipg
        if not self.port:
            self.port=9904 #still working on autoport

        # Set this to True if you have a non-buggy python OpenCv bindings--it greatly speeds up some operations
        self.use_opencv = opencv
        server = ipg.make_server(host='localhost',
                                 port=self.port,
                                 use_opencv=self.use_opencv,
                                 numthreads=threads)

        # Start viewer server
        # IMPORTANT: if running in an IPython/Jupyter notebook, use the no_ioloop=True option
        if 'nbagg' in get_backend().lower():
            server.start(no_ioloop=True)
        else:
            server.start(no_ioloop=False)

        self.ginga_view = server.get_viewer('ginga_view')
        self.figure=self.ginga_view #didn't find figure
        #self.figure.show()

        #pop up a separate browser window with the viewer
        self._open_browser()

        # create a canvas that we insert when doing imexam mode
        self.canvas = self.ginga_view.add_canvas()


    def close(self):
        """ close the window"""
        print("You must close the image window by hand")


    def grab(self):
            """
            copy current image to notebook
            """
            self.ginga_view.show()
