(* keep_hierarchy = "yes", dont_touch = "true" *)
module cdc_level_sync #(
    parameter integer WIDTH = 1
) (
    input  logic             destination_clock,
    input  logic             destination_reset_n,
    input  logic [WIDTH-1:0] async_level,
    output logic [WIDTH-1:0] synchronized_level
);
  (* async_reg = "true", keep = "true", dont_touch = "true" *)
  logic [WIDTH-1:0] first_stage;

  always_ff @(posedge destination_clock or negedge destination_reset_n) begin
    if (!destination_reset_n) begin
      first_stage        <= '0;
      synchronized_level <= '0;
    end else begin
      first_stage        <= async_level;
      synchronized_level <= first_stage;
    end
  end
endmodule
