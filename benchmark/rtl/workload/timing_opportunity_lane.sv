(* keep_hierarchy = "yes" *)
module timing_opportunity_lane #(
    parameter integer FAMILY = 0,
    parameter logic [31:0] SEED = 32'h1
) (
    input  logic [31:0] lane_input,
    output logic [31:0] lane_output
);
  logic [31:0] compare_mix;
  logic [31:0] arithmetic_mix;
  logic [31:0] reduction_mix;
  logic [31:0] selected_mix;

  always_comb begin
    compare_mix = lane_input ^ SEED;
    arithmetic_mix = (compare_mix + {SEED[15:0], SEED[31:16]})
                   ^ {compare_mix[6:0], compare_mix[31:7]};
    reduction_mix = arithmetic_mix
                  ^ {16{&arithmetic_mix[7:0], |arithmetic_mix[15:8]}}
                  ^ {arithmetic_mix[18:0], arithmetic_mix[31:19]};
    case (FAMILY)
      0: selected_mix = (reduction_mix[15:0] > SEED[15:0])
                      ? {reduction_mix[7:0], reduction_mix[31:8]}
                      : reduction_mix + 32'h1357_9bdf;
      1: selected_mix = reduction_mix
                      ^ ((reduction_mix[23:16] > reduction_mix[7:0])
                         ? 32'ha5a5_5a5a : 32'h5a5a_a5a5);
      2: selected_mix = (reduction_mix + {SEED[7:0], reduction_mix[31:8]})
                      ^ 32'h0101_1011;
      3: selected_mix = {reduction_mix[10:0], reduction_mix[31:11]}
                      + {16'b0, reduction_mix[31:16]};
      default: selected_mix = (reduction_mix[4:0] == SEED[4:0])
                            ? reduction_mix ^ 32'hc3c3_3c3c
                            : reduction_mix + 32'h3141_5927;
    endcase
    lane_output = selected_mix ^ {selected_mix[0], selected_mix[31:1]};
  end
endmodule
