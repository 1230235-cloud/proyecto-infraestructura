# -*- mode: ruby -*-
# vi: set ft=ruby :
#
# Cluster de 2 Maquinas Virtuales (VirtualBox) para la infraestructura CanSat:
#   - cansat-master  (192.168.56.10): Control plane k3s, Nginx LB, SSH/SFTP, Postfix
#   - cansat-worker1 (192.168.56.11): Nodo worker k3s + Docker

Vagrant.configure("2") do |config|
  config.vm.box = "ubuntu/jammy64"

  nodes = [
    { name: "cansat-master",  ip: "192.168.56.10", mem: 2048, cpus: 2 },
    { name: "cansat-worker1", ip: "192.168.56.11", mem: 1536, cpus: 2 },
  ]

  nodes.each do |node|
    config.vm.define node[:name] do |vm|
      vm.vm.hostname = node[:name]
      vm.vm.network "private_network", ip: node[:ip]
      vm.vm.synced_folder ".", "/vagrant"

      vm.vm.provider "virtualbox" do |vb|
        vb.name = node[:name]
        vb.memory = node[:mem]
        vb.cpus = node[:cpus]
      end
    end
  end
end
