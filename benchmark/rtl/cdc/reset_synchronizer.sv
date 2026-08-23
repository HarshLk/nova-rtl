(* keep_hierarchy = "yes", dont_touch = "true" *)
module reset_synchronizer (
    input  logic clock,
    input  logic async_reset_n,
    output logic local_reset_n
);
  (* async_reg = "true", keep = "true", dont_touch = "true" *) logic [1:0] release_pipe;

  always_ff @(posedge clock or negedge async_reset_n) begin
    if (!async_reset_n)
      release_pipe <= 2'b00;
    else
      release_pipe <= {release_pipe[0], 1'b1};
  end

  assign local_reset_n = release_pipe[1];
endmodule
