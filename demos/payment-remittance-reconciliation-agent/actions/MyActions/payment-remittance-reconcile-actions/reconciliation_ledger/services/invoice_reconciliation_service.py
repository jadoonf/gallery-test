from decimal import Decimal
from typing import Dict, List, Any
import logging
from datetime import datetime

from reconciliation_ledger.reconciliation_constants import DocumentStatus
from context.reconciliation_agent_context_manager import ReconciliationAgentContextManager
from reconciliation_ledger.db.invoice_loader import InvoiceLoader
from utils.commons.decimal_utils import DecimalHandler
from utils.commons.formatting import format_currency
from models.reconciliation_models import (
    DiscrepancySummary, FacilityAmountSummary, InvoiceDiscrepancyDetail, ProcessingMetrics,
    ReconciliationResult, RemittanceFields
)

class InvoiceReconciliationLedgerService:
    """
    Service for handling invoice reconciliation with the DuckDB ledger.
    """
    
    def __init__(self, config: Dict, context_manager):
        self.logger = logging.getLogger(__name__)
        self.context_manager = context_manager
        self.loader = InvoiceLoader(config.get('db_path'))

    def store_payment_with_allocations(
        self,
        payment_data: Dict[str, Any],
        invoices: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Store payment and its allocations."""
        customer_id = payment_data['customer_id']
        self.logger.info(f"Storing payment for customer {customer_id}")
        
        try:
            with self.loader.get_connection() as conn:
                # Parse monetary values
                total_payment = self._parse_monetary_value(payment_data['total_payment'])  # Net payment
                total_invoice = self._parse_monetary_value(payment_data['total_invoice_amount'])  # Gross AR
                total_discounts = self._parse_monetary_value(payment_data.get('total_discounts', 0))
                total_charges = self._parse_monetary_value(payment_data.get('total_charges', 0))
                
                # Create payment record
                payment_id = self._create_payment_record(conn, {
                    **payment_data,
                    'total_payment': float(total_payment),
                    'total_invoice_amount': float(total_invoice),
                    'total_discounts': float(total_discounts),
                    'total_charges': float(total_charges)
                })
                
                # Process allocations
                allocation_results = []
                facility_totals = {}
                
                for invoice in invoices:
                    # Parse invoice amounts
                    amount_paid = self._parse_monetary_value(invoice['Amount Paid'])  # Net amount
                    invoice_amount = self._parse_monetary_value(invoice['Invoice Amount'])  # Gross amount
                    discounts = self._parse_monetary_value(invoice.get('Discounts Applied', 0))
                    charges = self._parse_monetary_value(invoice.get('Additional Charges', 0))
                    
                    # Create allocation
                    allocation = self._create_allocation_record(
                        conn, customer_id, payment_id, {
                            **invoice,
                            'Amount Paid': float(amount_paid),
                            'Invoice Amount': float(invoice_amount),
                            'Discounts Applied': float(discounts),
                            'Additional Charges': float(charges)
                        }
                    )
                    allocation_results.append(allocation)
                    
                    # Track facility totals (using net amounts)
                    facility_type = invoice['Facility Type']
                    if facility_type not in facility_totals:
                        facility_totals[facility_type] = Decimal('0')
                    facility_totals[facility_type] += amount_paid

                # Convert facility totals to float with proper rounding
                facility_totals_float = {
                    k: float(v.quantize(Decimal('0.01')))
                    for k, v in facility_totals.items()
                }
                
                return {
                    "payment_id": payment_id,
                    "allocations": allocation_results,
                    "facility_totals": facility_totals_float,
                    "totals": {
                        "payment": float(total_payment),
                        "invoice": float(total_invoice),
                        "discounts": float(total_discounts),
                        "charges": float(total_charges)
                    }
                }
                
        except Exception as e:
            self.logger.error(f"Error storing payment: {str(e)}", exc_info=True)
            raise

    def analyze_payment_reconciliation(
        self,
        payment_reference: str,
        threshold: Decimal
    ) -> ReconciliationResult:
        """Analyze payment reconciliation with proper match status tracking."""
        try:
            with self.loader.get_connection() as conn:
                # Get basic payment info
                payment = self._get_payment_info(conn, payment_reference)
                customer_payment = payment['total_payment']
                
                # Calculate net AR balance
                ar_gross = payment['total_invoice_amount']
                ar_discounts = payment['total_discounts']
                total_ar_net = DecimalHandler.round_decimal(ar_gross - ar_discounts)
                
                # Get invoice data
                invoice_data = self._get_invoice_data(conn, payment['payment_id'])
                
                # First get base processing metrics
                processing_metrics = self._calculate_processing_metrics(invoice_data)
                
                # Check for discrepancies and update match status
                all_matched = True  # Start with True
                discrepancy_details = []
                
                for invoice in invoice_data:
                    remit_amount = invoice['allocated_amount']
                    ar_gross = invoice['ar_amount']
                    discounts = invoice.get('discounts', Decimal('0'))
                    
                    # Calculate net AR amount
                    ar_net = DecimalHandler.round_decimal(ar_gross - discounts)
                    difference = DecimalHandler.round_decimal(remit_amount - ar_net)
                    
                    if abs(difference) > threshold:
                        all_matched = False  # Set to False if any discrepancy found
                        discrepancy_details.append(InvoiceDiscrepancyDetail(
                            invoice_number=invoice['invoice_number'],
                            facility_id=invoice['facility_id'],
                            facility_type=invoice['facility_type'],
                            service_type=invoice['service_type'],
                            remittance_amount=remit_amount,
                            ar_amount=ar_net,
                            difference=difference
                        ))

                # Calculate facility summaries
                facility_summaries = self._calculate_facility_summaries(invoice_data, threshold)
                
                # Calculate total difference
                total_difference = DecimalHandler.round_decimal(customer_payment - total_ar_net)
                has_discrepancy = abs(total_difference) > threshold

                if has_discrepancy:
                    all_matched = False  # Also set to False if total amount has discrepancy

                # Update processing metrics with final match status
                processing_metrics['all_matched'] = all_matched
                
                # Create result
                result = ReconciliationResult(
                    status="DISCREPANCY_FOUND" if has_discrepancy else "MATCHED",
                    payment_reference=payment_reference,
                    payment_amount=customer_payment,
                    ar_balance=total_ar_net,
                    total_difference=total_difference,
                    processing_metrics=ProcessingMetrics(**processing_metrics),
                    discrepancy_summary=DiscrepancySummary(
                        total_difference=total_difference,
                        affected_facility_count=len([f for f in facility_summaries if f.has_discrepancy]),
                        affected_invoice_count=len(discrepancy_details),
                        total_remittance_amount=customer_payment,
                        total_ar_amount=total_ar_net,
                        facility_differences=facility_summaries,
                        affected_service_types=sorted(set(d.service_type for d in discrepancy_details))
                    ) if has_discrepancy else None,
                    invoice_discrepancies=discrepancy_details if has_discrepancy else None,
                    remittance_fields=self._get_remittance_fields(payment),
                    threshold=threshold
                )

                return result

        except Exception as e:
            self.logger.error(f"Error in reconciliation: {str(e)}", exc_info=True)
            raise


    def _calculate_facility_summaries(
        self, 
        invoice_data: List[Dict],
        threshold: Decimal
    ) -> List[FacilityAmountSummary]:
        """Calculate facility summaries using net amounts."""
        facility_totals = {}
        
        # First pass: aggregate amounts by facility
        for invoice in invoice_data:
            ftype = invoice['facility_type']
            if ftype not in facility_totals:
                facility_totals[ftype] = {
                    'remit_total': Decimal('0'),
                    'ar_gross': Decimal('0'),
                    'discounts': Decimal('0'),
                    'services': set(),
                    'count': 0,
                    'has_discrepancy': False  # Track discrepancies at facility level
                }
            
            ft = facility_totals[ftype]
            ft['remit_total'] += invoice['allocated_amount']
            ft['ar_gross'] += invoice['ar_amount']
            ft['discounts'] += invoice.get('discounts', Decimal('0'))
            ft['services'].add(invoice['service_type'])
            ft['count'] += 1

        # Second pass: create summaries
        summaries = []
        for ftype, totals in facility_totals.items():
            net_ar = DecimalHandler.round_decimal(
                totals['ar_gross'] - totals['discounts']
            )
            remit_total = DecimalHandler.round_decimal(totals['remit_total'])
            
            difference = DecimalHandler.round_decimal(remit_total - net_ar)
            has_discrepancy = abs(difference) > threshold
            
            summaries.append(FacilityAmountSummary(
                facility_type=ftype,
                remittance_amount=remit_total,
                ar_system_amount=net_ar,
                difference=difference,
                service_types=sorted(totals['services']),
                invoice_count=totals['count'],
                has_discrepancy=has_discrepancy
            ))

        return sorted(summaries, key=lambda x: abs(x.difference), reverse=True)
    
    
    def _calculate_processing_metrics(self, invoice_data: List[Dict]) -> Dict[str, Any]:
        """Calculate initial processing metrics."""
        if not invoice_data:
            return {
                "total_invoices": 0,
                "facility_types": [],
                "facility_type_count": 0,
                "service_types": [],
                "service_type_count": 0,
                "all_matched": False  # Default to False if no invoices
            }
            
        # Get unique facility types
        facility_types = sorted(set(inv['facility_type'] for inv in invoice_data))
        
        # Get unique service types
        service_types = sorted(set(inv['service_type'] for inv in invoice_data if inv['service_type']))
        
        return {
            "total_invoices": len(invoice_data),
            "facility_types": facility_types,
            "facility_type_count": len(facility_types),
            "service_types": service_types,
            "service_type_count": len(service_types),
            "all_matched": True  # Initial value, will be updated based on discrepancy checks
        }



    def _get_payment_info(self, conn, payment_reference: str) -> Dict[str, Any]:
        """Get payment information with proper decimal handling."""
        query = """
            SELECT 
                p.payment_id,
                p.customer_id,
                p.payment_date,
                p.bank_account_number,
                CAST(p.total_payment_paid AS DECIMAL(18, 2)) as total_payment,
                p.payment_method,
                CAST(p.total_invoice_amount AS DECIMAL(18, 2)) as total_invoice_amount,
                CAST(COALESCE(p.total_additional_charges, 0) AS DECIMAL(18, 2)) as total_charges,
                CAST(COALESCE(p.total_discounts_applied, 0) AS DECIMAL(18, 2)) as total_discounts,
                c.customer_name,
                p.remittance_notes
            FROM payment p
            JOIN customer c ON p.customer_id = c.customer_id
            WHERE p.payment_reference = ?
        """
        
        result = conn.execute(query, [payment_reference]).fetchone()
        if not result:
            raise ValueError(f"Payment not found: {payment_reference}")
            
        return {
            "payment_id": result[0],
            "customer_id": result[1],
            "payment_date": result[2],
            "bank_account": result[3],
            "total_payment": DecimalHandler.from_str(str(result[4])),  # Net payment
            "payment_method": result[5],
            "total_invoice_amount": DecimalHandler.from_str(str(result[6])),  # Gross AR
            "total_charges": DecimalHandler.from_str(str(result[7])),
            "total_discounts": DecimalHandler.from_str(str(result[8])),
            "customer_name": result[9],
            "remittance_notes": result[10]
        }

    def _get_invoice_data(self, conn, payment_id: str) -> List[Dict]:
        """Get invoice data with proper decimal handling."""
        query = """
            WITH payment_allocations AS (
                SELECT 
                    i.invoice_number,
                    i.invoice_id,
                    f.facility_id,
                    i.facility_type,
                    i.service_type,
                    CAST(pa.amount_applied AS DECIMAL(18, 2)) as allocated_amount,
                    CAST(i.invoice_amount AS DECIMAL(18, 2)) as ar_amount,
                    CAST(COALESCE(i.discounts_applied, 0) AS DECIMAL(18, 2)) as discounts
                FROM payment_allocation pa
                JOIN invoice i ON pa.invoice_id = i.invoice_id
                JOIN facility f ON i.internal_facility_id = f.internal_facility_id
                WHERE pa.payment_id = ?
            )
            SELECT *
            FROM payment_allocations
            ORDER BY facility_type, invoice_number
        """
        
        results = conn.execute(query, [payment_id]).fetchall()
        
        return [{
            "invoice_number": row[0],
            "invoice_id": row[1],
            "facility_id": row[2],
            "facility_type": row[3],
            "service_type": row[4],
            "allocated_amount": DecimalHandler.from_str(str(row[5])),  # Net amount
            "ar_amount": DecimalHandler.from_str(str(row[6])),         # Gross AR amount
            "discounts": DecimalHandler.from_str(str(row[7]))          # Discounts
        } for row in results]

    def _get_remittance_fields(self, payment: Dict) -> RemittanceFields:
        """Create RemittanceFields from payment data."""
        try:
            return RemittanceFields(
                customer_name=payment['customer_name'],
                customer_id=payment['customer_id'],
                payment_date=payment['payment_date'],  # Will be handled by validator
                payment_method=payment['payment_method'],
                payment_reference=payment.get('payment_reference', 'PMT-00000'),
                total_payment=payment['total_payment'],
                total_invoice_amount=payment['total_invoice_amount'],
                total_discounts=payment['total_discounts'],
                total_charges=payment['total_charges'],
                bank_account=payment['bank_account'],
                remittance_notes=payment.get('remittance_notes')
            )
        except Exception as e:
            self.logger.error(f"Error creating RemittanceFields: {str(e)}")
            raise ValueError(f"Failed to create RemittanceFields: {str(e)}")

    def _create_payment_record(
        self,
        conn,
        payment_data: Dict[str, Any]
    ) -> str:
        """Create payment record with proper decimal handling."""
                
        query = """
        INSERT INTO payment (
            payment_id, customer_id, payment_date, bank_account_number,
            total_payment_paid, payment_reference, payment_method,
            total_invoice_amount, total_additional_charges,
            total_discounts_applied, total_invoices, remittance_notes
        ) VALUES (?, ?, ?, ?, 
            CAST(? AS DECIMAL(18, 2)), ?, ?, 
            CAST(? AS DECIMAL(18, 2)), 
            CAST(? AS DECIMAL(18, 2)), 
            CAST(? AS DECIMAL(18, 2)), 
            ?, ?)
        ON CONFLICT (payment_id) DO UPDATE SET
            total_payment_paid = CAST(EXCLUDED.total_payment_paid AS DECIMAL(18, 2)),
            total_invoice_amount = CAST(EXCLUDED.total_invoice_amount AS DECIMAL(18, 2)),
            total_additional_charges = CAST(EXCLUDED.total_additional_charges AS DECIMAL(18, 2)),
            total_discounts_applied = CAST(EXCLUDED.total_discounts_applied AS DECIMAL(18, 2)),
            total_invoices = EXCLUDED.total_invoices,
            remittance_notes = EXCLUDED.remittance_notes
        """
        
        payment_id = f"PMT-{payment_data['payment_date']}-{payment_data['payment_reference']}"
        
        # Convert monetary values with proper decimal handling
        total_payment = DecimalHandler.from_str(str(payment_data['total_payment']))  # Net amount
        total_invoice = DecimalHandler.from_str(str(payment_data['total_invoice_amount']))  # Gross AR
        total_charges = DecimalHandler.from_str(str(payment_data.get('total_charges', 0)))
        total_discounts = DecimalHandler.from_str(str(payment_data.get('total_discounts', 0)))
        
        conn.execute(query, [
            payment_id,
            payment_data['customer_id'],
            payment_data['payment_date'],
            payment_data['bank_account'],
            float(total_payment),
            payment_data['payment_reference'],
            payment_data['payment_method'],
            float(total_invoice),
            float(total_charges),
            float(total_discounts),
            payment_data['invoice_count'],
            payment_data.get('remittance_notes', '')
        ])
        
        return payment_id

    def _create_allocation_record(
        self,
        conn,
        customer_id: str,
        payment_id: str,
        invoice: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Create allocation record with proper decimal handling."""
        # Get invoice_id
        query = """
        SELECT invoice_id 
        FROM invoice 
        WHERE customer_id = ? AND invoice_number = ?
        """
        
        result = conn.execute(query, [customer_id, invoice['Invoice Number']]).fetchone()
        if not result:
            raise ValueError(f"Invoice {invoice['Invoice Number']} not found for customer {customer_id}")
            
        invoice_id = result[0]
        
        # Create allocation with proper decimal handling
        query = """
        INSERT INTO payment_allocation (
            allocation_id, payment_id, invoice_id,
            amount_applied, invoice_amount, discounts_applied,
            additional_charges
        ) VALUES (?, ?, ?, 
            CAST(? AS DECIMAL(18, 2)), 
            CAST(? AS DECIMAL(18, 2)), 
            CAST(? AS DECIMAL(18, 2)), 
            CAST(? AS DECIMAL(18, 2)))
        ON CONFLICT (allocation_id) DO UPDATE SET
            amount_applied = CAST(EXCLUDED.amount_applied AS DECIMAL(18, 2)),
            invoice_amount = CAST(EXCLUDED.invoice_amount AS DECIMAL(18, 2)),
            discounts_applied = CAST(EXCLUDED.discounts_applied AS DECIMAL(18, 2)),
            additional_charges = CAST(EXCLUDED.additional_charges AS DECIMAL(18, 2))
        """
        
        allocation_id = f"ALLOC-{payment_id}-{invoice_id}"
        
        # Parse amounts ensuring proper decimal handling
        amount_applied = DecimalHandler.from_str(str(invoice['Amount Paid']))  # Net amount
        invoice_amount = DecimalHandler.from_str(str(invoice['Invoice Amount']))  # Gross amount
        discounts = DecimalHandler.from_str(str(invoice.get('Discounts Applied', 0)))
        charges = DecimalHandler.from_str(str(invoice.get('Additional Charges', 0)))
        
        conn.execute(query, [
            allocation_id,
            payment_id,
            invoice_id,
            float(amount_applied),
            float(invoice_amount),
            float(discounts),
            float(charges)
        ])
        
        return {
            "allocation_id": allocation_id,
            "invoice_id": invoice_id,
            "invoice_number": invoice['Invoice Number'],
            "amount": float(amount_applied),
            "invoice_amount": float(invoice_amount),
            "discounts": float(discounts),
            "charges": float(charges)
        }
        
    def _parse_monetary_value(self, value: Any) -> Decimal:
        """Parse monetary values with consistent decimal handling."""
        if isinstance(value, str):
            # Remove currency symbol and commas
            clean_value = value.replace('$', '').replace(',', '')
            return DecimalHandler.from_str(clean_value)
        return DecimalHandler.from_str(str(value))