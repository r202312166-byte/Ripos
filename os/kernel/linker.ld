/* Order the Limine request sections so Limine's ELF scan finds
   requests_start < requests < requests_end. */
SECTIONS {
  .requests_start : { *(.requests_start) }
  .requests : { *(.requests) }
  .requests_end : { *(.requests_end) }
}
INSERT AFTER .text;
