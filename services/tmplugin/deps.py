"""Imports shared by the package's modules."""
import json
import os
import re
import struct
import sys
import time

import titles
from titles import core as _core

import tetramaster
import tmauction
import tmfixtures
import tmrank
import tmroom
import tmsave


#: The flat module's path. Code that looked for data files beside it
#: (`os.path.dirname(__file__)`) looks beside the facade, as before.
FACADE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "tmtitle.py")
