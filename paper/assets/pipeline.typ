#import "@preview/fletcher:0.5.8" as fletcher: diagram, node, edge

#let pipeline-diagram = block()[
  #set text(size: 6pt)
  #diagram(debug: false, spacing: (10em, 4em), (
    node((0, 0), name: <ghrepo>, corner-radius: 5pt, width: 10em, stroke: 0.5pt, fill: white)[GitHub\ Repositories],
    node((1,0), name: <bins>, corner-radius: 5pt, width: 10em, stroke: 0.5pt, fill: white)[
      default \ debug (`-N -l`) \ stripped (`-s -w`)
      #place(dy:-4.8em, center)[Binaries]
    ],
    node(enclose: (<ghrepo>, <og-source>),stroke: (paint: gray.darken(25%), dash: (3pt, 35pt, ..((3pt,)*100)), thickness: 0.5pt), fill: gray.lighten(75%), corner-radius: 5pt, inset: 1.2em)[],
    node(enclose: (<ghrepo>, <bins>), fill: gray.lighten(75%), corner-radius: 5pt, stroke: (paint: gray.darken(25%), dash: (..((3pt,)*13), 81pt, ..((3pt,)*100)), thickness: 0.5pt), inset: 1.2em)[
      #place(dx: -0.5em, dy: -2.2em, text(gray.darken(25%))[*Repository Selection*])
    ], 
    edge(<ghrepo>,<bins>, "-stealth", label-sep: 0em)[Compilation],
    node((2,0), name: <sym-bins>, corner-radius: 5pt, width: 10em, stroke: 0.5pt, fill: white)[
      Binaries + Symbols
    ],
    node((2,1), name: <pseudocode>, corner-radius: 5pt, width: 10em, stroke: 0.5pt, fill: white)[
      C Pseudocode
    ],
    node((2,2), name: <str-pseudocode>, corner-radius: 5pt, width: 10em, stroke: 0.5pt, fill: white)[
      Annotated  Pseudocode
    ],
    edge(<bins>,<sym-bins>, "-stealth", label-sep: 0em)[GoReSym],
    edge(<sym-bins>,<pseudocode>, "-stealth", label-sep: 0em)[Ghidra],
    edge(<pseudocode>,<str-pseudocode>, "-stealth", label-sep: 0em, align(right)[String\ Recovery]),
    node(enclose: (<sym-bins>, <pseudocode>, <str-pseudocode>), fill: blue.lighten(75%), corner-radius: 5pt, stroke: (paint: blue.darken(25%), dash: "dashed", thickness: 0.5pt), inset: 1em)[
      #place(dx: -0.5em, dy: -2em, text(blue.darken(25%))[*Decompilation*])
    ],
    node((1,2), name: <chunked-decomps>, corner-radius: 5pt, width: 10em, stroke: 0.5pt, fill: white)[
      Chunked\ Decompilations
    ],
    node((1,1), name: <inferred-source>, corner-radius: 5pt, width: 10em, stroke: 0.5pt, fill: white)[
      Inferred Go Source
    ],
    edge(<str-pseudocode>,<chunked-decomps>, "-stealth", label-sep: 0em)[Function chunking],
    edge(<chunked-decomps>,<inferred-source>, "-stealth", label-sep: 0em)[LLM\ Inference],

    node(enclose: (<chunked-decomps>, <inferred-source>), fill: green.lighten(75%), corner-radius: 5pt, stroke: (paint: green.darken(25%), dash: "dashed", thickness: 0.5pt), inset: 1em)[
      #place(dx: -0.5em, dy: -2em, text(green.darken(25%))[*LLM Inference*])
    ],
    node((0,1), name: <og-source>, corner-radius: 5pt, width: 10em, stroke: 0.5pt, fill: white)[
      Chunked\ Go Source
    ],
    node((0,2), name: <output>, corner-radius: 5pt, width: 10em, stroke: 0.5pt, fill: white)[
      CodeBLEU \
      LLM-as-a-judge\
      Syntax validity
    ],
    edge(<ghrepo>,<og-source>, "-stealth", label-sep: 0em)[Function\ chunking],

    edge(<og-source>,<inferred-source>, "-", label-sep: 0em)[Evaluation],
    edge((0.5, 1),(0.5,2),<output.east>, "-stealth"),

    node(enclose: (<output>,), fill: orange.lighten(75%), corner-radius: 5pt, stroke: (paint: orange.darken(25%), dash: "dashed", thickness: 0.5pt), inset: 1em)[
      #place(dx: -0.5em, dy: -2em, text(orange.darken(25%))[*Results*])
    ],
  )),
] 