SECTIONS {
  . = 0;
  .text : { *(.text) *(.text.*) }
  .rodata : {
    *(.rodata) *(.rodata.*)
    *(.rdata) *(.rdata.*) *(.rdata$*)
    *(.eh_frame*) *(.got) *(.got.*)
  }
  .data : { *(.data) *(.data.*) }
  .bss : { *(.bss) *(.bss.*) *(COMMON) }
}
