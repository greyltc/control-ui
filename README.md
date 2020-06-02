# control-ui
control system user interface

## Testing
1. Install [Vagrant](https://www.vagrantup.com/downloads)
1. Install [Virtual Box](https://www.virtualbox.org/wiki/Downloads)
1. Clone this repo and open a command terminal in its top folder
1. Run the following commands:
   ```bash
   cd vgrant
   vagrant up # ignore the virtual box terminal window that pops up here
   vagrant ssh
   sudo pacman -Syu reflector
   sudo reflector --latest 5 --sort rate --save /etc/pacman.d/mirrorlist
   sudo pacman -Syyuu
   sudo pacman -Syu  virtualbox-guest-utils # choose y (to replace the nox version)
   sudo reboot
   sudo pacman -Syyu xf86-video-vmware # choose y (to replace the nox version)
   bash-completion
   
   ```
1. A virtual box window should appear. Login with user/pass: vagrant/vagrant
1. Now in the virtual machine's terminal run:
   ```bash
   sudo pacman -Syyu virtualbox-guest-utils # choose y (to replace the nox version)
   sudo reboot
   # virtual machine reboots ... login again as the vagrant user
   sudo pacman -Syyu gnome git bash-completion vim # accept all defaults
   sudo systemctl enable gdm
   sudo reboot
   ```
