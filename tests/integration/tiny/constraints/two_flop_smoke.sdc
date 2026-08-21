current_design nova_m0_two_flop_smoke

create_clock -name smoke_clock -period 1000 [get_ports clk]
set_input_delay 100 -clock smoke_clock [get_ports {reset_n data_in}]
set_output_delay 100 -clock smoke_clock [get_ports data_out]
