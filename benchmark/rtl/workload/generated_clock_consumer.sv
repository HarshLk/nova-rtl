(* keep_hierarchy = "yes", dont_touch = "true" *)
module generated_clock_consumer #(
    parameter logic [7:0] SEED = 8'h1
) (
    input  logic generated_clock,
    input  logic async_reset_n,
    output logic active_state
);
  logic generated_reset_n;

  (* keep = "true", dont_touch = "true" *)
  reset_synchronizer u_generated_reset (
      .clock(generated_clock),
      .async_reset_n(async_reset_n),
      .local_reset_n(generated_reset_n));

  (* keep = "true", dont_touch = "true" *) logic [7:0] protected_state;

  always_ff @(posedge generated_clock or negedge generated_reset_n) begin
    if (!generated_reset_n) begin
      protected_state <= SEED;
      active_state    <= 1'b0;
    end else begin
      protected_state <= {protected_state[6:0],
                          protected_state[7] ^ protected_state[5]
                          ^ protected_state[4] ^ protected_state[3]};
      active_state <= active_state | (|protected_state);
    end
  end
endmodule
