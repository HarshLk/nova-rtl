(* keep_hierarchy = "yes", dont_touch = "true" *)
module clock_divider_bank #(
    parameter integer ACTIVE_DIVIDERS = 21
) (
    input  logic        master_clock,
    input  logic        local_reset_n,
    output logic [20:0] generated_clocks
);
  if (ACTIVE_DIVIDERS > 0) begin : gen_div_02
    (* keep = "true", dont_touch = "true" *)
    protected_clock_divider #(.RATIO(2)) u_divider (
        .master_clock(master_clock), .local_reset_n(local_reset_n),
        .generated_clock(generated_clocks[0]));
  end else begin : gen_no_div_02 assign generated_clocks[0] = 1'b0; end
  if (ACTIVE_DIVIDERS > 1) begin : gen_div_03
    (* keep = "true", dont_touch = "true" *)
    protected_clock_divider #(.RATIO(3)) u_divider (
        .master_clock(master_clock), .local_reset_n(local_reset_n),
        .generated_clock(generated_clocks[1]));
  end else begin : gen_no_div_03 assign generated_clocks[1] = 1'b0; end
  if (ACTIVE_DIVIDERS > 2) begin : gen_div_04
    (* keep = "true", dont_touch = "true" *)
    protected_clock_divider #(.RATIO(4)) u_divider (
        .master_clock(master_clock), .local_reset_n(local_reset_n),
        .generated_clock(generated_clocks[2]));
  end else begin : gen_no_div_04 assign generated_clocks[2] = 1'b0; end
  if (ACTIVE_DIVIDERS > 3) begin : gen_div_05
    (* keep = "true", dont_touch = "true" *)
    protected_clock_divider #(.RATIO(5)) u_divider (
        .master_clock(master_clock), .local_reset_n(local_reset_n),
        .generated_clock(generated_clocks[3]));
  end else begin : gen_no_div_05 assign generated_clocks[3] = 1'b0; end
  if (ACTIVE_DIVIDERS > 4) begin : gen_div_06
    (* keep = "true", dont_touch = "true" *)
    protected_clock_divider #(.RATIO(6)) u_divider (
        .master_clock(master_clock), .local_reset_n(local_reset_n),
        .generated_clock(generated_clocks[4]));
  end else begin : gen_no_div_06 assign generated_clocks[4] = 1'b0; end
  if (ACTIVE_DIVIDERS > 5) begin : gen_div_07
    (* keep = "true", dont_touch = "true" *)
    protected_clock_divider #(.RATIO(7)) u_divider (
        .master_clock(master_clock), .local_reset_n(local_reset_n),
        .generated_clock(generated_clocks[5]));
  end else begin : gen_no_div_07 assign generated_clocks[5] = 1'b0; end
  if (ACTIVE_DIVIDERS > 6) begin : gen_div_08
    (* keep = "true", dont_touch = "true" *)
    protected_clock_divider #(.RATIO(8)) u_divider (
        .master_clock(master_clock), .local_reset_n(local_reset_n),
        .generated_clock(generated_clocks[6]));
  end else begin : gen_no_div_08 assign generated_clocks[6] = 1'b0; end
  if (ACTIVE_DIVIDERS > 7) begin : gen_div_10
    (* keep = "true", dont_touch = "true" *)
    protected_clock_divider #(.RATIO(10)) u_divider (
        .master_clock(master_clock), .local_reset_n(local_reset_n),
        .generated_clock(generated_clocks[7]));
  end else begin : gen_no_div_10 assign generated_clocks[7] = 1'b0; end
  if (ACTIVE_DIVIDERS > 8) begin : gen_div_12
    (* keep = "true", dont_touch = "true" *)
    protected_clock_divider #(.RATIO(12)) u_divider (
        .master_clock(master_clock), .local_reset_n(local_reset_n),
        .generated_clock(generated_clocks[8]));
  end else begin : gen_no_div_12 assign generated_clocks[8] = 1'b0; end
  if (ACTIVE_DIVIDERS > 9) begin : gen_div_16
    (* keep = "true", dont_touch = "true" *)
    protected_clock_divider #(.RATIO(16)) u_divider (
        .master_clock(master_clock), .local_reset_n(local_reset_n),
        .generated_clock(generated_clocks[9]));
  end else begin : gen_no_div_16 assign generated_clocks[9] = 1'b0; end
  if (ACTIVE_DIVIDERS > 10) begin : gen_div_20
    (* keep = "true", dont_touch = "true" *)
    protected_clock_divider #(.RATIO(20)) u_divider (
        .master_clock(master_clock), .local_reset_n(local_reset_n),
        .generated_clock(generated_clocks[10]));
  end else begin : gen_no_div_20 assign generated_clocks[10] = 1'b0; end
  if (ACTIVE_DIVIDERS > 11) begin : gen_div_24
    (* keep = "true", dont_touch = "true" *)
    protected_clock_divider #(.RATIO(24)) u_divider (
        .master_clock(master_clock), .local_reset_n(local_reset_n),
        .generated_clock(generated_clocks[11]));
  end else begin : gen_no_div_24 assign generated_clocks[11] = 1'b0; end
  if (ACTIVE_DIVIDERS > 12) begin : gen_div_25
    (* keep = "true", dont_touch = "true" *)
    protected_clock_divider #(.RATIO(25)) u_divider (
        .master_clock(master_clock), .local_reset_n(local_reset_n),
        .generated_clock(generated_clocks[12]));
  end else begin : gen_no_div_25 assign generated_clocks[12] = 1'b0; end
  if (ACTIVE_DIVIDERS > 13) begin : gen_div_32
    (* keep = "true", dont_touch = "true" *)
    protected_clock_divider #(.RATIO(32)) u_divider (
        .master_clock(master_clock), .local_reset_n(local_reset_n),
        .generated_clock(generated_clocks[13]));
  end else begin : gen_no_div_32 assign generated_clocks[13] = 1'b0; end
  if (ACTIVE_DIVIDERS > 14) begin : gen_div_40
    (* keep = "true", dont_touch = "true" *)
    protected_clock_divider #(.RATIO(40)) u_divider (
        .master_clock(master_clock), .local_reset_n(local_reset_n),
        .generated_clock(generated_clocks[14]));
  end else begin : gen_no_div_40 assign generated_clocks[14] = 1'b0; end
  if (ACTIVE_DIVIDERS > 15) begin : gen_div_48
    (* keep = "true", dont_touch = "true" *)
    protected_clock_divider #(.RATIO(48)) u_divider (
        .master_clock(master_clock), .local_reset_n(local_reset_n),
        .generated_clock(generated_clocks[15]));
  end else begin : gen_no_div_48 assign generated_clocks[15] = 1'b0; end
  if (ACTIVE_DIVIDERS > 16) begin : gen_div_50
    (* keep = "true", dont_touch = "true" *)
    protected_clock_divider #(.RATIO(50)) u_divider (
        .master_clock(master_clock), .local_reset_n(local_reset_n),
        .generated_clock(generated_clocks[16]));
  end else begin : gen_no_div_50 assign generated_clocks[16] = 1'b0; end
  if (ACTIVE_DIVIDERS > 17) begin : gen_div_64
    (* keep = "true", dont_touch = "true" *)
    protected_clock_divider #(.RATIO(64)) u_divider (
        .master_clock(master_clock), .local_reset_n(local_reset_n),
        .generated_clock(generated_clocks[17]));
  end else begin : gen_no_div_64 assign generated_clocks[17] = 1'b0; end
  if (ACTIVE_DIVIDERS > 18) begin : gen_div_80
    (* keep = "true", dont_touch = "true" *)
    protected_clock_divider #(.RATIO(80)) u_divider (
        .master_clock(master_clock), .local_reset_n(local_reset_n),
        .generated_clock(generated_clocks[18]));
  end else begin : gen_no_div_80 assign generated_clocks[18] = 1'b0; end
  if (ACTIVE_DIVIDERS > 19) begin : gen_div_96
    (* keep = "true", dont_touch = "true" *)
    protected_clock_divider #(.RATIO(96)) u_divider (
        .master_clock(master_clock), .local_reset_n(local_reset_n),
        .generated_clock(generated_clocks[19]));
  end else begin : gen_no_div_96 assign generated_clocks[19] = 1'b0; end
  if (ACTIVE_DIVIDERS > 20) begin : gen_div_128
    (* keep = "true", dont_touch = "true" *)
    protected_clock_divider #(.RATIO(128)) u_divider (
        .master_clock(master_clock), .local_reset_n(local_reset_n),
        .generated_clock(generated_clocks[20]));
  end else begin : gen_no_div_128 assign generated_clocks[20] = 1'b0; end
endmodule
