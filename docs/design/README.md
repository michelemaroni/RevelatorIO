# Alternate EQ rack references

These are original visual references for the Linux Host's Passive Program EQ
and Vintage EQ panels. They were developed from the io24 control topology
recovered from Universal Control 4.7.2 and the Host's existing rack language.
They are design references, not copied Universal Control bitmaps and not
runtime dependencies.

- `passive-program-eq-reference.png` establishes the blue steel faceplate,
  low/high grouping, recessed response display, stepped selectors, screws and
  power lamp.
- `vintage-eq-reference.png` establishes the dark 1970s faceplate, colored
  band knobs, dividers, recessed response display, rack ears and power lamp.

The implemented panel intentionally replaces any illustrative scale text with
the exact decoded io24 values. Every knob changes its matching semantic field,
the response display is calculated from the same coefficients sent by the
Host, and the lamp follows that EQ model's independent on/off state.
