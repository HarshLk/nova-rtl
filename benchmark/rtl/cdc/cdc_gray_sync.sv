(* keep_hierarchy = "yes", dont_touch = "true" *)
module cdc_gray_sync #(
    parameter integer WIDTH = 3
) (
    input  logic             destination_clock,
    input  logic             destination_reset_n,
    input  logic [WIDTH-1:0] async_gray,
    output logic [WIDTH-1:0] synchronized_gray
);
  (* async_reg = "true", keep = "true", dont_touch = "true" *)
  logic [WIDTH-1:0] first_stage;

  always_ff @(posedge destination_clock or negedge destination_reset_n) begin
    if (!destination_reset_n) begin
      first_stage       <= '0;
      synchronized_gray <= '0;
    end else begin
      first_stage       <= async_gray;
      synchronized_gray <= first_stage;
    end
  end
endmodule
