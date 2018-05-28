#!/usr/bin/env python2
# coding: utf-8
# Copyright 2013 The Font Bakery Authors. All Rights Reserved.
# Copyright 2017 The Google Font Tools Authors
# Copyright 2018 The Font Classification Tool Authors:
#                - Felipe C. da S. Sanches
#                - Dave Crossland
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Initially authored by Google and contributed by Filip Zembowicz.
# Further improved by Dave Crossland and Felipe Sanches.
#
# OVERVIEW + USAGE
#
# font-classification-tool.py -h
#
import argparse
import collections
import csv
import glob
import math
import os
import StringIO
import sys
import re
import errno
from fonts_public_pb2 import FamilyProto
from constants import (NAMEID_FONT_FAMILY_NAME,
                       NAMEID_FONT_SUBFAMILY_NAME)

DESCRIPTION = """Calculates the visual weight, width or italic angle of fonts.

  For width, it just measures the width of how a particular piece of text renders.

  For weight, it measures the darkness of a piece of text.

  For italic angle it defaults to the italicAngle property of the font.

  Then it starts a HTTP server and shows you the results, or
  if you pass --debug then it just prints the values.

  Example (all Google Fonts files, all existing data):
    font-classification-tool.py --files="fonts/*/*/*.ttf" --existing=fonts/tools/font-metadata.csv
"""

parser = argparse.ArgumentParser(description=DESCRIPTION)
parser.add_argument("-f", "--files", default="*", required=True, nargs="+",
                    help="The pattern to match for finding ttfs, eg 'folder_with_fonts/*.ttf'.")
parser.add_argument("-d", "--debug", default=False, action='store_true',
                    help="Debug mode, just print results")
parser.add_argument("-e", "--existing", default=False,
                    help="Path to existing font-metadata.csv")
parser.add_argument("-m", "--missingmetadata", default=False, action='store_true',
                    help="Only process fonts for which metadata is not available yet")
parser.add_argument("-o", "--output", default="output.csv", required=True,
                    help="CSV data output filename")

#TODO: make these available as CLI arguments as well:
VERBOSE=True

try:
  from PIL import (Image,
                   ImageDraw,
                   ImageFont)
except:
  sys.exit("Needs pillow.\n\npip install pillow")


try:
  from fontTools.ttLib import TTFont
except:
  sys.exit("Needs fontTools.\n\npip install fonttools")

try:
  from google.protobuf import text_format
except:
  sys.exit("Needs protobuf.\n\npip install protobuf")

try:
  from flask import (Flask,
                     jsonify,
                     request,
                     send_from_directory)

except:
  sys.exit("Needs flask.\n\npip install flask")

# The font size used to test for weight and width.
FONT_SIZE = 30

# The text used to test weight and width. Note that this could be
# problematic if a given font doesn't have latin support.
TEXT = "AaBbCcDdEeFfGgHhIiJjKkLlMmNnOoPpQqRrSsTtUuVvXxYyZz"

# Fonts that cause problems: any filenames containing these letters
# will be skipped.
# TODO: Investigate why these don't work.
BLACKLIST = [
#IOError: execution context too long (issue #703)
  "Padauk",
  "KumarOne",
#ZeroDivisionError: float division by zero
  "AdobeBlank",
  "Phetsarath",
# IOError: invalid reference See also: https://github.com/google/fonts/issues/132#issuecomment-244796023
  "Corben",
# IOError: stack overflow on text_width, text_height = font.getsize(TEXT) 
  "Rubik",
]

def generate_italic_angle_images():
  for i in range(10):
    angle = 30*(float(i)/10) * 3.1415/180
    width = 2000
    height = 500
    lines = 250
    im = Image.new('RGBA', (width,height), (255,255,255,0))
    spacing = width/lines
    draw = ImageDraw.Draw(im)
    for j in range(lines):
      draw.line([j*spacing - 400, im.size[1], j*spacing - 400 + im.size[1]*math.tan(angle), 0], fill=(50,50,255,255))
    del draw

    imagesdir = os.path.join(os.path.dirname(__file__), "font_classification_tool", "images")
    if not os.path.isdir(imagesdir):
       os.mkdir(imagesdir)
    filepath = os.path.join(imagesdir, "angle_{}.png".format(i+1))
    im.save(filepath, "PNG")


def get_FamilyProto_Message(path):
    message = FamilyProto()
    text_data = open(path, "rb").read()
    text_format.Merge(text_data, message)
    return message


def normalize_values(properties, target_max=1.0):
  """Normalizes a set of values from 0 to target_max"""
  max_value = 0.0
  for i in range(len(properties)):
    val = float(properties[i]['value'])
    max_value = max(max_value, val)
  for i in range(len(properties)):
    properties[i]['value'] *= (target_max/max_value)


def map_to_int_range(values, target_min=1, target_max=10):
  """Maps a list into the integer range from target_min to target_max
     Pass a list of floats, returns the list as ints
     The 2 lists are zippable"""
  integer_values = []
  values_ordered = sorted(values)
  min_value = float(values_ordered[0])
  max_value = float(values_ordered[-1])

  if min_value == max_value:
    #convert to integer and clamp between min and max
    integer_value = int(min_value)
    integer_value = max(target_min, integer_value)
    integer_value = min(integer_value, target_max)
    return [integer_value for v in values]

  target_range = (target_max - target_min)
  float_range = (max_value - min_value)
  for value in values:
    value = target_min + int(target_range * ((value - min_value) / float_range))
    integer_values.append(value)
  return integer_values

ITALIC_ANGLE_TEMPLATE = """
<img height='30%%' src='data:image/png;base64,%s'
     style="background:url(font_classification_tool/images/angle_%d.png) 0 0 no-repeat;" />
"""

# The canonical [to Google Fonts] name comes before any aliases
_KNOWN_WEIGHTS = collections.OrderedDict([
    ('Thin', 100),
    ('Hairline', 100),
    ('ExtraLight', 200),
    ('Light', 300),
    ('Regular', 400),
    ('', 400),  # Family-Italic resolves to this
    ('Medium', 500),
    ('SemiBold', 600),
    ('Bold', 700),
    ('ExtraBold', 800),
    ('Black', 900)
])

FileFamilyStyleWeightTuple = collections.namedtuple(
    'FileFamilyStyleWeightTuple', ['file', 'family', 'style', 'weight'])


def StyleWeight(styleweight):
  """Breaks apart a style/weight specifier into a 2-tuple of (style, weight).

  Args:
    styleweight: style/weight string, e.g. Bold, Regular, or ExtraLightItalic.
  Returns:
    2-tuple of style (normal or italic) and weight.
  """
  if styleweight.endswith('Italic'):
    return ('italic', _KNOWN_WEIGHTS[styleweight[:-6]])

  return ('normal', _KNOWN_WEIGHTS[styleweight])


def FamilyName(fontname):
  """Attempts to build family name from font name.

  For example, HPSimplifiedSans => HP Simplified Sans.

  Args:
    fontname: The name of a font.
  Returns:
    The name of the family that should be in this font.
  """
  # SomethingUpper => Something Upper
  fontname = re.sub('(.)([A-Z][a-z]+)', r'\1 \2', fontname)
  # Font3 => Font 3
  fontname = re.sub('([a-z])([0-9]+)', r'\1 \2', fontname)
  # lookHere => look Here
  return re.sub('([a-z0-9])([A-Z])', r'\1 \2', fontname)


class ParseError(Exception):
  """Exception used when parse failed."""


def FileFamilyStyleWeight(filename):
  """Extracts family, style, and weight from Google Fonts standard filename.

  Args:
    filename: Font filename, eg Lobster-Regular.ttf.
  Returns:
    FileFamilyStyleWeightTuple for file.
  Raises:
    ParseError: if file can't be parsed.
  """

  m = re.search(r'([^/-]+)-(\w+)\.ttf$', filename) #FAMILY_WEIGHT_REGEX
  if not m:
    raise ParseError('Could not parse %s' % filename)

  sw = StyleWeight(m.group(2))
  return FileFamilyStyleWeightTuple(filename,
                                    FamilyName(m.group(1)),
                                    sw[0],
                                    sw[1])


def _FileFamilyStyleWeights(fontdir):
  """Extracts file, family, style, weight 4-tuples for each font in dir.

  Args:
    fontdir: Directory that supposedly contains font files for a family.
  Returns:
    List of FileFamilyStyleWeightTuple ordered by weight, style
    (normal first).
  Raises:
    OSError: If the font directory doesn't exist (errno.ENOTDIR) or has no font
    files (errno.ENOENT) in it.
    RuntimeError: If the font directory appears to contain files from multiple
    families.
  """
  if not os.path.isdir(fontdir):
    raise OSError(errno.ENOTDIR, 'No such directory', fontdir)

  files = glob.glob(os.path.join(fontdir, '*.ttf'))
  if not files:
    raise OSError(errno.ENOENT, 'no font files found')

  result = [FileFamilyStyleWeight(f) for f in files]
  def _Cmp(r1, r2):
    return cmp(r1.weight, r2.weight) or -cmp(r1.style, r2.style)
  result = sorted(result, _Cmp)

  family_names = {i.family for i in result}
  if len(family_names) > 1:
    raise RuntimeError('Ambiguous family name; possibilities: %s'
                       % family_names)

  return result


def get_gfn(fontfile, ttfont):
  gfn = "unknown"
  fontdir = os.path.dirname(fontfile)
  metadata = os.path.join(fontdir, "METADATA.pb")
  if os.path.exists(metadata):
    family = get_FamilyProto_Message(metadata)
    for font in family.fonts:
      if font.filename in fontfile:
        gfn = "{}:{}:{}".format(family.name, font.style, font.weight)
        break
  else:
    try:
      attributes = _FileFamilyStyleWeights(fontdir)
      for (fontfname, family, style, weight) in attributes:
        if fontfname in fontfile:
          gfn = "{}:{}:{}".format(family, style, weight)
          break
    except:
      pass

  if gfn == 'unknown':
    #This font lacks a METADATA.pb file and also failed
    # to auto-detect the GFN value. As a last resort
    # we'll try to extract the info from the NAME table entries.
    try:
      for entry in ttfont['name'].names:
        if entry.nameID == NAMEID_FONT_FAMILY_NAME:
          family = entry.string.decode(entry.getEncoding()).encode('ascii', 'ignore').strip()
        if entry.nameID == NAMEID_FONT_SUBFAMILY_NAME:
          style, weight = StyleWeight(entry.string.decode(entry.getEncoding()).encode('ascii', 'ignore').strip())
      ttfont.close()
      if family != "": #avoid empty string in cases of misbehaved family names in the name table
        gfn = "{}:{}:{}".format(family, style, weight)
        if VERBOSE:
          print ("Detected GFN from name table entries: '{}' (file='{}')".format(gfn, fontfile))
    except:
      print("This seems to be a really bad font file...")
      pass

  if gfn == 'unknown':
    print ("Failed to detect GFN value for '{}'. Defaults to 'unknown'.".format(fontfile))

  return gfn


blacklisted = []
def analyse_fonts(files):
  """Returns fontinfo dict"""
  global blacklisted

  fontinfo = {}
  # run the analysis for each file, in sorted order
  for count, fontfile in enumerate(sorted(files)):
    # if blacklisted the skip it
    if is_blacklisted(fontfile):
      blacklisted.append(fontfile)
      print >> sys.stderr, "[{}/{}] {} BLACKLISTED!".format(count+1, len(files), fontfile)
      continue
    else:
      print("[{}/{}] {}...".format(count+1, len(files), fontfile))
    # put metadata in dictionary
    ttfont = TTFont(fontfile)
    darkness, width, img_d = get_darkness_and_width(fontfile)
    angle = get_angle(ttfont)
    gfn = get_gfn(fontfile, ttfont)
    ttfont.close()
    fontinfo[gfn] = {"weight": darkness,
                     "width": width,
                     "angle": angle,
                     "img_weight": img_d,
                     "usage": "unknown",
                     "gfn": gfn,
                     "fontfile": fontfile
                    }
  return fontinfo



def is_blacklisted(filename):
  """Returns whether a font is on the blacklist."""

  # first check for explicit blacklisting:
  for name in BLACKLIST:
    if name in filename:
      return True


def get_angle(ttfont):
  """Returns the italic angle, given a filename of a TTF"""
  return ttfont['post'].italicAngle


def get_darkness_and_width(fontfile):
  """Returns the darkness and width if a given a TTF.
     Width is in pixels so it should be normalized."""

  # Render the test text using the font onto an image.
  font = ImageFont.truetype(fontfile, FONT_SIZE)
  text_width, text_height = font.getsize(TEXT)
  img = Image.new('RGBA', (text_width, text_height))
  draw = ImageDraw.Draw(img)
  draw.text((0, 0), TEXT, font=font, fill=(0, 0, 0))

  # Calculate the average darkness.
  histogram = img.histogram()
  avg = 0.0
  for i in range(256):
    alpha = histogram[i + 3*256]
    avg += (i / 255.0) * alpha

  darkness = avg / (text_width * text_height)
  return darkness, text_width, get_base64_image(img)


def get_base64_image(img):
  """Get the base 64 representation of an image,
     to use for visual testing."""
  output = StringIO.StringIO()
  img.save(output, "PNG")
  base64img = output.getvalue().encode("base64")
  output.close()
  return base64img


def get_x_height(fontfile):
  """Returns the height of the lowercase "x" in a font."""
  font = ImageFont.truetype(fontfile, FONT_SIZE)
  _, x_height = font.getsize("x")
  return x_height


def render_slant_chars(fontfile):
  """Renders a sample of a few glyphs and
     returns a PNG image as base64 data"""
  # Disable this to speedup the tool
  # We are currently not using this image
  return ""

  SAMPLE_CHARS = "HNHNUHNHN"
  font = ImageFont.truetype(fontfile, FONT_SIZE * 10)
  try:
    text_width, text_height = font.getsize(SAMPLE_CHARS)
  except:
    text_width, text_height = 1, 1
  img = Image.new('RGBA', (text_width, 20+text_height))
  draw = ImageDraw.Draw(img)
  try:
    draw.text((0, 0), SAMPLE_CHARS, font=font, fill=(0, 0, 0))
  except:
    pass
  return get_base64_image(img)


def main():
  args = parser.parse_args()

  if len(sys.argv) < 2:
    parser.print_help()
    sys.exit(-1)

  files_to_process = []
  for pattern in args.files:
    files_to_process.extend(glob.glob(pattern))

  if len(files_to_process) == 0:
    sys.exit("No font files were found!")

  if args.missingmetadata:
    if args.existing == False:
      sys.exit("you must use the --existing attribute in conjunction with --missingmetadata")
    else:
      rejected = []
      with open(args.existing) as csvfile:
        existing_data = csv.reader(csvfile, delimiter=',', quotechar='"')
        next(existing_data) # skip first row as its not data
        for row in existing_data:
          name = row[0].split(':')[0]
          if ' ' in name:
            name = ''.join(name.split(' '))
          for f, fname in enumerate(files_to_process):
            if name in fname:
              files_to_process.pop(f)
              rejected.append(fname + ":" + row[0])

      print("These files were removed from the list:\n" + '\n'.join(rejected))

  # analyse fonts
  fontinfo = analyse_fonts(files_to_process)

  if fontinfo == {}:
    sys.exit("All specified fonts are blacklisted!")


  # normalise weights
  weights = []
  for key in sorted(fontinfo.keys()):
    weights.append(fontinfo[key]["weight"])
  ints = map_to_int_range(weights)
  #print("weights: {}".format(weights))
  #print("ints: {}".format(ints))
  for count, key in enumerate(sorted(fontinfo.keys())):
    fontinfo[key]['weight_int'] = ints[count]

  # normalise widths
  widths = []
  for key in sorted(fontinfo.keys()):
    widths.append(fontinfo[key]["width"])
  ints = map_to_int_range(widths)
  for count, key in enumerate(sorted(fontinfo.keys())):
    fontinfo[key]['width_int'] = ints[count]

  # normalise angles
  angles = []
  for gfn in sorted(fontinfo.keys()):
    angle = abs(fontinfo[gfn]["angle"])
    angles.append(angle)
    #print("gfn: {} angles: {}".format(gfn, angle))
  ints = map_to_int_range(angles)
  for count, key in enumerate(sorted(fontinfo.keys())):
    fontinfo[key]['angle_int'] = ints[count]

  # include existing values
  if args.existing and args.missingmetadata == False:
    with open(args.existing) as csvfile:
        existing_data = csv.reader(csvfile, delimiter=',', quotechar='"')
        next(existing_data) # skip first row as its not data
        for row in existing_data:
          gfn = row[0]
          if gfn in fontinfo.keys():
            fontinfo[gfn]["weight_int"] = int(row[1])
            fontinfo[gfn]["angle_int"] = int(row[2])
            fontinfo[gfn]["width_int"] = int(row[3])
            fontinfo[gfn]["usage"] = row[4]
            #print("got this one! keys='{}', gfn='{}'".format(fontinfo.keys(), gfn))

  # if we are debugging, just print the stuff
  if args.debug:
    items = ["weight", "weight_int", "width", "width_int",
             "angle", "angle_int", "usage", "gfn"]
    for key in sorted(fontinfo.keys()):
       print fontinfo[key]["fontfile"],
       for item in items:
         print fontinfo[key][item],
       print ""
    sys.exit(0)

  # generate data for the web server
  # double(<unit>, <precision>, <decimal_point>, <thousands_separator>, <show_unit_before_number>, <nansymbol>)
  grid_data = {
    "metadata": [
      {"name":"fontfile","label":"filename","datatype":"string","editable":True},
      {"name":"gfn","label":"GFN","datatype":"string","editable":True},
      {"name":"weight","label":"weight","datatype":"double(, 2, dot, comma, 0, n/a)","editable":True},
      {"name":"weight_int","label":"WEIGHT_INT","datatype":"integer","editable":True},
      {"name":"width","label":"width","datatype":"double(, 2, dot, comma, 0, n/a)","editable":True},
      {"name":"width_int","label":"WIDTH_INT","datatype":"integer","editable":True},
      {"name":"usage","label":"USAGE","datatype":"string","editable":True,
        "values": {"header":"header", "body":"body", "unknown":"unknown"}
      },
      {"name":"angle","label":"angle","datatype":"double(, 2, dot, comma, 0, n/a)","editable":True},
      {"name":"angle_int","label":"ANGLE_INT","datatype":"integer","editable":True},
      {"name":"image","label":"image","datatype":"html","editable":False},
    ],
    "data": []
  }
  generate_italic_angle_images()

  field_id = 1
  for key in fontinfo:
    values = fontinfo[key]
    if values["gfn"] == "unknown":
      continue
    img_weight_html = ""
    if values["img_weight"] is not None:
      img_weight_html = "<img height='50%%' src='data:image/png;base64,%s' />" % (values["img_weight"])

    img_angle_html = ITALIC_ANGLE_TEMPLATE % (render_slant_chars(values["fontfile"]), values["angle_int"])

    values["image"] = img_weight_html
    values["angle_image"] = img_angle_html
    grid_data["data"].append({"id": field_id, "values": values})
    field_id += 1

  def save_csv():
    filename = args.output
    with open(filename, 'w') as csvfile:
        writer = csv.writer(csvfile, delimiter=',', quotechar='"', lineterminator='\n')
        writer.writerow(["GFN","FWE","FIA","FWI","USAGE"]) # first row has the headers
        for data in sorted(grid_data['data'], key=lambda d: d['values']['gfn']):
          values = data['values']
          gfn = values['gfn']
          fwe = values['weight_int']
          fia = values['angle_int']
          fwi = values['width_int']
          usage = values['usage']
          writer.writerow([gfn, fwe, fia, fwi, usage])
    return 'ok'

  app = Flask(__name__)

  @app.route('/font_classification_tool/<path:path>')
  def send_js(path):
    return send_from_directory(os.path.dirname(__file__) + '/font_classification_tool/', path)

  @app.route('/data.json')
  def json_data():
    return jsonify(grid_data)

  @app.route('/update', methods=['POST'])
  def update():
    rowid = request.form['id']
    newvalue = request.form['newvalue']
    colname = request.form['colname']
    for row in grid_data["data"]:
      if row['id'] == int(rowid):
        row['values'][colname] = newvalue
    return save_csv()

  if blacklisted:
    print ("{} blacklisted font files:\n".format(len(blacklisted)))
    print ("".join(map("* {}\n".format, blacklisted)))

  print ("\n\nAccess http://127.0.0.1:5000/font_classification_tool/index.html\n")
  app.run()


if __name__ == "__main__":
  main()
