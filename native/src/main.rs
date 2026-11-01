#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    if kontur_offline_lib::run().is_err() {
        kontur_offline_lib::show_startup_error();
    }
}
