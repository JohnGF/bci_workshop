# -*- mode: ruby -*-
# vi: set ft=ruby :

Vagrant.configure("2") do |config|
  # Use Ubuntu 22.04 LTS (Jammy Jellyfish) as the base box
  config.vm.box = "ubuntu/jammy64"

  # Forward X11 so GUI apps can run seamlessly if the host supports it
  config.ssh.forward_x11 = true

  # Configure VirtualBox specific settings
  config.vm.provider "virtualbox" do |vb|
    # Give the VM a name
    vb.name = "Muse_Workshop_VM"

    # Enable GUI to show the desktop environment
    vb.gui = true

    # Allocate CPU and Memory
    vb.memory = "4096"
    vb.cpus = 2

    # Enable 3D Acceleration (Requires VMSVGA in VirtualBox 7.x+)
    vb.customize ["modifyvm", :id, "--graphicscontroller", "vmsvga"]
    vb.customize ["modifyvm", :id, "--vram", "128"]
    vb.customize ["modifyvm", :id, "--accelerate-3d", "on"]

    # Enable Audio (VirtualBox 7.x Syntax)
    vb.customize ["modifyvm", :id, "--audio-driver", "default"]
    vb.customize ["modifyvm", :id, "--audio-enabled", "on"]
    vb.customize ["modifyvm", :id, "--audio-out", "on"]
    vb.customize ["modifyvm", :id, "--audio-in", "on"]

    # Enable USB Controller for Bluetooth Passthrough
    vb.customize ["modifyvm", :id, "--usb", "on"]
    vb.customize ["modifyvm", :id, "--usbehci", "on"]
  end

  # Sync the current directory to /app in the VM
  config.vm.synced_folder ".", "/app"

  # Provision the VM using the setup script
  config.vm.provision "shell", inline: <<-SHELL
    # Make sure setup_vm.sh has correct line endings in case cloned on Windows
    sed -i 's/\\r$//' /app/setup_vm.sh

    # Run the setup script as the default 'vagrant' user
    sudo -u vagrant -i bash -c "cd /app && ./setup_vm.sh"

    # Install a lightweight desktop environment (XFCE) to view the GUI directly in the VirtualBox window
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y xfce4 xfce4-goodies

    # Ensure startx boots into XFCE
    echo "startxfce4" | sudo -u vagrant tee /home/vagrant/.xinitrc

    echo "=========================================================="
    echo "Vagrant Provisioning Complete!"
    echo "1. Run 'vagrant reload' to reboot the VM."
    echo "2. The VirtualBox GUI will appear. Login with user: vagrant, pass: vagrant"
    echo "3. Run 'startx' to launch the Desktop Environment."
    echo "4. Open a terminal and run: cd /app && uv run main.py"
    echo "=========================================================="
  SHELL
end