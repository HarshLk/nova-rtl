module nova_m0_two_flop_smoke (
    input  logic clk,
    input  logic reset_n,
    input  logic data_in,
    output logic data_out
);
    logic first_stage;

    always_ff @(posedge clk) begin
        if (!reset_n) begin
            first_stage <= 1'b0;
            data_out    <= 1'b0;
        end else begin
            first_stage <= data_in;
            data_out    <= first_stage;
        end
    end
endmodule
