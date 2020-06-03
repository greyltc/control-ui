# control-ui
control system user interface

## Testing
1. Install [Vagrant](https://www.vagrantup.com/downloads)
1. Install [Virtual Box](https://www.virtualbox.org/wiki/Downloads)
1. Clone this repo and open a command terminal in its top folder
1. Run the following commands:
   ```bash
   cd vgrant
   vagrant up # ignore the virtual box terminal window that pops up here for now
   vagrant ssh
   sudo reflector --latest 5 --sort rate --save /etc/pacman.d/mirrorlist
   sudo pacman -Syyuu
   reboot
   vagrant ssh
   sudo pacman -Syu virtualbox-guest-utils # choose y (to replace the nox version)
   sudo pacman -Syu xf86-video-vmware mate git bash-completion vim gdm termite gedit # accept all defaults
   sudo systemctl enable gdm
   sudo echo -e "[User]\nXSession=mate\nSystemAccount=false" > /var/lib/AccountsService/users/vagrant
   mkdir git
   cd git
   git clone https://github.com/greyltc/control-ui.git
   cd control-ui
   ./get_deps.sh
   sudo reboot
   ```
1. A virtual box window should have appeared a few commands ago. Login with user/pass: vagrant/vagrant
1. Navigate to git/control-ui in the vagrant user's home folder and double click on the run_local.sh script
1. Use The System --> Shut Down menu to turn off the virtual machine and `vagrant up` to start it again
