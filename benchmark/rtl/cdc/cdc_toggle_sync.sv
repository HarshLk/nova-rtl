(* keep_hierarchy = "yes", dont_touch = "true" *)
module cdc_toggle_sync (
    input  logic source_clock,
    input  logic source_reset_n,
    input  logic source_pulse,
    input  logic destination_clock,
    input  logic destination_reset_n,
    output logic destination_pulse,
    output logic observed_synchronized_toggle,
    output logic observed_destination_toggle
);
  (* keep = "true", dont_touch = "true" *) logic source_toggle;
  (* async_reg = "true", keep = "true", dont_touch = "true" *) logic [1:0] toggle_pipe;
  logic destination_toggle;

  always_ff @(posedge source_clock or negedge source_reset_n) begin
    if (!source_reset_n)
      source_toggle <= 1'b0;
    else if (source_pulse)
      source_toggle <= ~source_toggle;
  end

  always_ff @(posedge destination_clock or negedge destination_reset_n) begin
    if (!destination_reset_n) begin
      toggle_pipe        <= 2'b00;
      destination_toggle <= 1'b0;
      destination_pulse  <= 1'b0;
    end else begin
      toggle_pipe        <= {toggle_pipe[0], source_toggle};
      destination_toggle <= toggle_pipe[1];
      destination_pulse  <= toggle_pipe[1] ^ destination_toggle;
    end
  end

  assign observed_synchronized_toggle = toggle_pipe[1];
  assign observed_destination_toggle = destination_toggle;
endmodule
