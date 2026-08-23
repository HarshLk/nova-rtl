(* keep_hierarchy = "yes", dont_touch = "true" *)
module cdc_req_ack #(
    parameter integer WIDTH = 32
) (
    input  logic             source_clock,
    input  logic             source_reset_n,
    input  logic [WIDTH-1:0] source_data,
    input  logic             source_valid,
    output logic             source_busy,
    input  logic             destination_clock,
    input  logic             destination_reset_n,
    output logic [WIDTH-1:0] destination_data,
    output logic             destination_valid,
    output logic             observed_request_toggle,
    output logic             observed_acknowledge_toggle
);
  (* keep = "true", dont_touch = "true" *) logic [WIDTH-1:0] source_data_hold;
  (* keep = "true", dont_touch = "true" *) logic request_toggle;
  (* keep = "true", dont_touch = "true" *) logic acknowledge_toggle;
  (* async_reg = "true", keep = "true", dont_touch = "true" *) logic [1:0] request_pipe;
  (* async_reg = "true", keep = "true", dont_touch = "true" *) logic [1:0] acknowledge_pipe;

  assign source_busy = request_toggle ^ acknowledge_pipe[1];

  always_ff @(posedge source_clock or negedge source_reset_n) begin
    if (!source_reset_n) begin
      source_data_hold <= '0;
      request_toggle   <= 1'b0;
      acknowledge_pipe <= 2'b00;
    end else begin
      acknowledge_pipe <= {acknowledge_pipe[0], acknowledge_toggle};
      if (source_valid && !source_busy) begin
        source_data_hold <= source_data;
        request_toggle   <= ~request_toggle;
      end
    end
  end

  always_ff @(posedge destination_clock or negedge destination_reset_n) begin
    if (!destination_reset_n) begin
      request_pipe       <= 2'b00;
      acknowledge_toggle <= 1'b0;
      destination_data   <= '0;
      destination_valid  <= 1'b0;
    end else begin
      request_pipe      <= {request_pipe[0], request_toggle};
      destination_valid <= 1'b0;
      if (request_pipe[1] != acknowledge_toggle) begin
        destination_data   <= source_data_hold;
        destination_valid  <= 1'b1;
        acknowledge_toggle <= request_pipe[1];
      end
    end
  end

  assign observed_request_toggle = request_toggle;
  assign observed_acknowledge_toggle = acknowledge_pipe[1];
endmodule
