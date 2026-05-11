#let string-recovery = block[
  #set text(size: 6pt)
  #set align(left)
  #grid(
    columns: (1fr,),
    block(radius: (top: 2pt), stroke: 0.5pt + blue.darken(25%), fill: blue.lighten(75%), width: 100%, inset: (x: 0.5em, y: 0.5em))[
      #set text(fill: blue.darken(50%))
      *Heuristic 1: Defined Data Scan*
    ],
    block(radius: (bottom: 2pt), stroke: 0.5pt + blue.darken(25%), fill: gray.lighten(90%), width: 100%)[
      #block(inset: (x: 0.5em, y: 0.5em))[
        Scans Ghidra's typed items once per binary. `s_*` labels yield strings directly; `PTR_s_*` labels are pointer-dereferenced.
      ]
      #v(-2.5em)
      #line(length: 100%, stroke: stroke(paint: blue.darken(25%), dash: "dashed", thickness: 0.5pt))
      #v(-2.5em)
      #block(inset: (y: 0.5em))[
        #set text(size: 4pt, weight: "semibold")

        #text(fill: gray.darken(25%))[
        `From tailscale/tailscale - client/systray.setAppIcon (stripped build):`
        ]
        
        #text(green.darken(50%))[
          `// Strings:`
          
          `//   (@`#text(blue.darken(25%))[`s_loading_00f87a900`]`@) = "loading"`
        ]

        #v(1em)
        #text(fill: gray.darken(25%))[`C pseudocode - recovered string used in comparison:`]

        #text(blue.darken(50%))[`if`]` ((in_stack_00000008 == `#text(blue.darken(50%))[`s_loading_00f87a900`]`)`
        
        `   && (DAT_00f7a908 == cStack0000000000000010)) {`

        `  tailscale_com_client_systray_startLoadingAnimation();`
        
        `}`
        #v(1em)
        #text(fill:gray.darken(25%), weight: "regular")[
          Ghidra typed *`s_loading`* as string data at *`0xf7a900`*; the pipeline reads "loading" directly from that address.
        ]
      ]
    ]
  )

  #v(-0.5em)
  
  #grid(
    columns: (1fr,),
    block(radius: (top: 2pt), stroke: 0.5pt + orange.darken(25%), fill: orange.lighten(75%), width: 100%, inset: (x: 0.5em, y: 0.5em))[
      #set text(fill: orange.darken(50%))
      *Heuristic 2: Undefined Data Resolution*
    ],
    block(radius: (bottom: 2pt), stroke: 0.5pt + orange.darken(25%), fill: gray.lighten(90%), width: 100%)[
      #block(inset: (x: 0.5em, y: 0.5em))[
        Resolves `PTR_s_* / s_*` references in C pseudocode absent from the data listing. Parses hex suffix from name; reads `Go(ptr, len)` pairs for precise, length-delimited extraction.
      ]
      #v(-2.5em)
      #line(length: 100%, stroke: stroke(paint: orange.darken(25%), dash: "dashed", thickness: 0.5pt))
      #v(-2.5em)
      #block(inset: (y: 0.5em))[
        #set text(size: 4pt, weight: "semibold")

        #text(fill: gray.darken(25%))[
        `From usememos/memos - plugin/filter.NewAttachmentSchema (default build):`
        ]
        
        #text(green.darken(50%))[
          `// Strings:`
          
          `//   (@`#text(orange.darken(25%))[`PTR_s_attachmentindex_html_.._01aad170`]`@)  = ["attachment", "memo_id"]`

          `//   (@`#text(orange.darken(25%))[`PTR_s_attachmentindex_html_.._01aacfc0`]`@)  = ["attachment", "filename",`

          `//    "mime_type", "scalar", "string", "attachment", "type",`

          `//    "create_time", "scalar", "timestamp", ...]`

        ]

        #v(1em)
        #text(fill: gray.darken(25%))[`C pseudocode referencing the symbols (initializing map entries):`]

        `local_68 = `
        #text(orange.darken(50%))[`PTR_s_attachmentindex_html_.._01aacfc0`]`;`

        `uStack_60 = _UNK_01aacfc8;`

        ` ...`

        `runtime_mapassign(in_RDI, in_RSI, key, &PTR_DAT_01a76720);`
        
        #v(1em)
        #text(fill:gray.darken(25%), weight: "regular")[
          Hex suffix *`_01aacfc0`* parsed #sym.arrow.r memory at *`0x1aacfc0`* read as *`Go(ptr, len)`* pairs #sym.arrow.r 17 domain-specific strings recovered.
        ]
      ]
    ]
  )
  
  #v(-0.5em)
  
  #grid(
    columns: (1fr,),
    block(radius: (top: 2pt), stroke: 0.5pt + purple.darken(25%), fill: purple.lighten(75%), width: 100%, inset: (x: 0.5em, y: 0.5em))[
      #set text(fill: purple.darken(50%))
      *Heuristic 3: DAT Symbols*
    ],
    block(radius: (bottom: 2pt), stroke: 0.5pt + purple.darken(25%), fill: gray.lighten(90%), width: 100%)[
      #block(inset: (x: 0.5em, y: 0.5em))[
        Resolves `DAT_*` labels (unclassified data). Detects paired string lengths from adjacent stack locals or function arguments.
      ]
      #v(-2.5em)
      #line(length: 100%, stroke: stroke(paint: purple.darken(25%), dash: "dashed", thickness: 0.5pt))
      #v(-2.5em)
      #block(inset: (y: 0.5em))[
        #set text(size: 4pt, weight: "semibold")

        #text(fill: gray.darken(25%))[
        `From ollama/ollama - main.main.func1 in pull-progress (default build):`
        ]
        
        #text(green.darken(50%))[
          `// Strings:`
          
          `//   (@`#text(purple.darken(25%))[`DAT_007ac68b`]`@)  = "Progress: status=%v, total=%v, completed=%v"`
        ]

        #v(1em)
        #text(fill: gray.darken(25%))[`C pseudocode - used as a format string argument:`]

        `w.tab = (internal_abi_ITab *)0x2c;    `#text(purple.darken(25%))[`//length = 44`]

        `format.len = (`#text(blue.darken(25%))[`int`]`)&`#text(purple.darken(25%))[`DAT_007ac68b`]`;`

        `format.str = extraout_RDX;`

        `fmt_Fprintf(w, format, in_stack, 3, ...);`
        
        #v(1em)
        #text(fill:gray.darken(25%), weight: "regular")[
          Hex address parsed from label. Length *`0x2c`* (44) detected from adjacent assignment, matching the 44-byte format string.
        ]
      ]
    ]
  )

  #v(-0.5em)
    
  #grid(
    columns: (1fr,),
    block(radius: (top: 2pt), stroke: 0.5pt + red.darken(25%), fill: red.lighten(75%), width: 100%, inset: (x: 0.5em, y: 0.5em))[
      #set text(fill: red.darken(50%))
      *Heuristic 4: Hex Literals*
    ],
    block(radius: (bottom: 2pt), stroke: 0.5pt + red.darken(25%), fill: gray.lighten(90%), width: 100%)[
      #block(inset: (x: 0.5em, y: 0.5em))[
        Resolves bare hex constants in C pseudocode that fall within `.rodata` address ranges.
      ]
      #v(-2.5em)
      #line(length: 100%, stroke: stroke(paint: red.darken(25%), dash: "dashed", thickness: 0.5pt))
      #v(-2.5em)
      #block(inset: (y: 0.5em))[
        #set text(size: 4pt, weight: "semibold")

        #text(fill: gray.darken(25%))[
        `From argoproj/argo-workflows - v1alpha1.ClusterWorkflowTemplate.GetResourceScope (stripped build):`
        ]
        
        #text(green.darken(50%))[
          `// Strings:`
          
          `//   (@`#text(red.darken(25%))[`0x3171032`]`@)  = "cluster"`
        ]

        #v(1em)
        #text(fill: gray.darken(25%))[`C pseudocode - bare hex address returned as a string pointer:`]

        #text(blue.darken(25%))[`undefined8`]` ...GetResourceScope(`#text(blue.darken(25%))[`void`]`) {`

        #text(blue.darken(25%))[`  return `]#text(red.darken(25%))[`0x3171032`]`;`

        `}`
        
        #v(1em)
        #text(fill:gray.darken(25%), weight: "regular")[
          Address *`0x3171032`* falls within *`.rodata`*. The decompiler shows only a numeric constant; Heuristic 4 resolves it to *`"cluster"`*.
        ]
      ]
    ]
  )
]