# control-ui
control system user interface

## Testing
1. Install [Vagrant](https://www.vagrantup.com/downloads)
1. Install [Virtual Box](https://www.virtualbox.org/wiki/Downloads)
1. Clone this repo and open a command terminal in its top folder
1. Run the following commands:
   ```bash
   cd vgrant
   vagrant up
   ```
1. A virtual box window should appear. Login with user/pass: vagrant/vagrant
1. Now in the virtual machine's terminal run:
   ```bash
   sudo pacman -Syyu virtualbox-guest-utils # choose y (to replace the nox version)
   sudo reboot
   # virtual machine reboots ... login again as the vagrant user
   sudo pacman -Syyu gnome git # accept all defaults
   sudo systemctl enable gdm
   sudo reboot
   ```
