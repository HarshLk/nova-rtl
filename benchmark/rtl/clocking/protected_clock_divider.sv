(* keep_hierarchy = "yes", dont_touch = "true" *)
module protected_clock_divider #(
    parameter integer RATIO = 2
) (
    input  logic master_clock,
    input  logic local_reset_n,
    output logic generated_clock
);
  localparam integer LOW_SPAN = RATIO / 2;
  localparam integer HIGH_SPAN = RATIO - LOW_SPAN;
  localparam integer COUNT_WIDTH = (RATIO <= 2) ? 1 : $clog2(RATIO);

  (* keep = "true", dont_touch = "true" *) logic [COUNT_WIDTH-1:0] span_count;
  logic [COUNT_WIDTH-1:0] active_span;

  always_comb begin
    active_span = generated_clock ? COUNT_WIDTH'(HIGH_SPAN) : COUNT_WIDTH'(LOW_SPAN);
  end

  always_ff @(posedge master_clock or negedge local_reset_n) begin
    if (!local_reset_n) begin
      span_count      <= '0;
      generated_clock <= 1'b0;
    end else if (span_count == active_span - 1'b1) begin
      span_count      <= '0;
      generated_clock <= ~generated_clock;
    end else begin
      span_count <= span_count + 1'b1;
    end
  end
endmodule
