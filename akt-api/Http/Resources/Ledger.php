<?php

namespace Modules\AktApi\Http\Resources;

use Illuminate\Http\Resources\Json\JsonResource;

class Ledger extends JsonResource
{
    /**
     * Transform the resource into an array.
     *
     * Rows come from the query builder (plain objects), so properties are
     * accessed directly.
     *
     * @param  \Illuminate\Http\Request  $request
     * @return array
     */
    public function toArray($request)
    {
        return [
            'id'              => $this->id,
            'company_id'      => $this->company_id,
            'account_id'      => $this->account_id,
            'ledgerable_type' => $this->ledgerable_type,
            'ledgerable_id'   => $this->ledgerable_id,
            'entry_type'      => $this->entry_type,
            'debit'           => $this->debit,
            'credit'          => $this->credit,
            'issued_at'       => $this->issued_at,
        ];
    }
}
