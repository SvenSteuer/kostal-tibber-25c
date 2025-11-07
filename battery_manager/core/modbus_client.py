#!/usr/bin/env python3
"""
Kostal Modbus TCP Client
Portiert von batcharge.py

Steuert die Batterie-Ladeleistung über Modbus Register 1034
"""

import logging
from pymodbus.client.tcp import ModbusTcpClient
from pymodbus.constants import Endian
from pymodbus.payload import BinaryPayloadBuilder

logger = logging.getLogger(__name__)


class ModbusClient:
    """
    Modbus TCP Client for Kostal Plenticore Plus
    
    Schreibt Batterie-Ladeleistung auf Register 1034 (Float32)
    Negativ = Laden, Positiv = Entladen, 0 = Automatik
    """
    
    def __init__(self, inverter_ip, port=1502, slave_id=71):
        """
        Initialize Modbus Client
        
        Args:
            inverter_ip: IP-Adresse des Wechselrichters
            port: Modbus TCP Port (Standard: 1502)
            slave_id: Modbus Slave ID (Standard: 71)
        """
        self.inverter_ip = inverter_ip
        self.port = port
        self.slave_id = slave_id
        self.client = None
        self.connected = False
        
        logger.info(f"Modbus Client initialized for {inverter_ip}:{port}, Slave ID {slave_id}")
    
    def connect(self):
        """
        Establish Modbus TCP connection
        
        Returns:
            bool: True if successful
        """
        try:
            if self.client and self.client.is_socket_open():
                logger.debug("Modbus connection already established")
                return True
            
            self.client = ModbusTcpClient(
                self.inverter_ip,
                port=self.port,
                timeout=5
            )
            
            result = self.client.connect()
            self.connected = result
            
            if result:
                logger.info(f"Modbus connection established to {self.inverter_ip}:{self.port}")
            else:
                logger.error(f"Failed to connect to Modbus at {self.inverter_ip}:{self.port}")
            
            return result
            
        except Exception as e:
            logger.error(f"Modbus connection error: {e}")
            self.connected = False
            return False
    
    def disconnect(self):
        """Close Modbus connection"""
        try:
            if self.client:
                self.client.close()
                self.connected = False
                logger.info("Modbus connection closed")
        except Exception as e:
            logger.warning(f"Error closing Modbus connection: {e}")
    
    def write_battery_power(self, power_watts):
        """
        Write battery charge/discharge power to Register 1034
        
        Args:
            power_watts: Power in Watts
                        Negative = Charging (e.g., -3900 = charge with 3900W)
                        Positive = Discharging (e.g., 2000 = discharge with 2000W)
                        0 = Automatic mode (back to internal control)
        
        Returns:
            bool: True if successful
        """
        try:
            # Ensure connection
            if not self.connected:
                if not self.connect():
                    logger.error("Cannot write - not connected")
                    return False
            
            # Verify connection is still open
            if not self.client.is_socket_open():
                logger.warning("Connection lost, reconnecting...")
                if not self.connect():
                    return False
            
            # Prepare Float32 payload (Big Endian, Little Word Order)
            builder = BinaryPayloadBuilder(
                byteorder=Endian.BIG,
                wordorder=Endian.LITTLE
            )
            builder.add_32bit_float(float(power_watts))
            payload = builder.build()
            
            # Write to Register 1034 (Battery charge power setpoint)
            result = self.client.write_registers(
                address=1034,
                values=payload,
                slave=self.slave_id,
                skip_encode=True
            )
            
            if result.isError():
                logger.error(f"Modbus write error: {result}")
                return False
            else:
                action = "Charging" if power_watts < 0 else "Discharging" if power_watts > 0 else "Automatic"
                logger.info(f"Battery power set to {power_watts}W ({action})")
                return True
                
        except Exception as e:
            logger.error(f"Error writing battery power: {e}")
            return False
    
    def start_charging(self, power_watts):
        """
        Start charging battery
        
        Args:
            power_watts: Charging power in Watts (positive number, will be negated)
        
        Returns:
            bool: True if successful
        """
        # Ensure power is negative for charging
        charge_power = -abs(power_watts)
        return self.write_battery_power(charge_power)
    
    def stop_charging(self):
        """
        Stop charging - return to automatic mode
        
        Returns:
            bool: True if successful
        """
        return self.write_battery_power(0)
    
    def start_discharging(self, power_watts):
        """
        Start discharging battery
        
        Args:
            power_watts: Discharging power in Watts (positive number)
        
        Returns:
            bool: True if successful
        """
        # Ensure power is positive for discharging
        discharge_power = abs(power_watts)
        return self.write_battery_power(discharge_power)
    
    def read_register(self, address, count=2, data_type='float32'):
        """
        Read Modbus register(s)
        
        Args:
            address: Register address
            count: Number of registers to read
            data_type: Data type ('float32', 'int32', 'uint32', etc.)
        
        Returns:
            Value or None if failed
        """
        try:
            if not self.connected:
                if not self.connect():
                    return None
            
            result = self.client.read_holding_registers(
                address=address,
                count=count,
                slave=self.slave_id
            )
            
            if result.isError():
                logger.error(f"Modbus read error: {result}")
                return None
            
            # Parse based on data type
            from pymodbus.payload import BinaryPayloadDecoder
            decoder = BinaryPayloadDecoder.fromRegisters(
                result.registers,
                byteorder=Endian.BIG,
                wordorder=Endian.LITTLE
            )
            
            if data_type == 'float32':
                return decoder.decode_32bit_float()
            elif data_type == 'int32':
                return decoder.decode_32bit_int()
            elif data_type == 'uint32':
                return decoder.decode_32bit_uint()
            else:
                logger.warning(f"Unknown data type: {data_type}")
                return None
                
        except Exception as e:
            logger.error(f"Error reading register {address}: {e}")
            return None
    
    def test_connection(self):
        """Test Modbus connection"""
        try:
            if self.connect():
                # Try to read a register to verify communication
                # Register 1068 = Battery SOC
                soc = self.read_register(1068, count=2, data_type='float32')
                if soc is not None:
                    logger.info(f"Modbus test successful, Battery SOC: {soc}%")
                    return True
            return False
        except Exception as e:
            logger.error(f"Modbus test failed: {e}")
            return False
    
    def __del__(self):
        """Cleanup on destruction"""
        self.disconnect()
