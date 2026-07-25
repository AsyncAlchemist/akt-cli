<?php

namespace Modules\AktApi\Http\Resources;

use Illuminate\Http\Resources\Json\JsonResource;

class Balance extends JsonResource
{
    public function toArray($request)
    {
        return [
            'account_id' => (int) $this->account_id,
            'debit'      => (float) $this->debit,
            'credit'     => (float) $this->credit,
        ];
    }
}
